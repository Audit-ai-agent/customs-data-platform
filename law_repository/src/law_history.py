"""Explicit, local-only 65-law Evidence history profile.

This module deliberately reimplements the small parts needed by the current
V3 Data architecture.  It does not import, execute, or adopt the separately
cloned ``trade-doc-auditor`` repository.  Official law bodies are fetched only
through the existing allowlisted ``lawgo`` client, retained under the ignored
profile directory, and verified again before serving.
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
import re
import secrets
import shutil
import tempfile
from contextlib import contextmanager
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast

from src import repository
from src.evidence import (
    DatabaseEvidenceBackend,
    LawArticleMatch,
    TemporalCoverage,
    TemporalContext,
    TemporalEvidenceSupport,
)
from src.hsk_ccct_history import ActiveArchiveCatalog
from src.functional_uat import (
    _safe_directory,
    _verify_cached_models,
    canonical_json_bytes,
    prefetch_models,
    sha256_bytes,
)
from src.law_history_spec import (
    LAW_HISTORY_BASELINE_AS_OF,
    LAW_HISTORY_CCCT_DOCUMENT_COUNT,
    LAW_HISTORY_CCCT_INPUT,
    LAW_HISTORY_CCCT_QUERY_COUNT,
    LAW_HISTORY_CCCT_RAW_ROW_COUNT,
    LAW_HISTORY_CCCT_REQUIREMENT_COUNT,
    LAW_HISTORY_CCCT_SNAPSHOT_ID,
    LAW_HISTORY_FROZEN_INPUTS_VERSION,
    LAW_HISTORY_HSK_INPUTS,
    LAW_HISTORY_HSK_LEAF_COUNT,
    LAW_HISTORY_HSK_NODE_COUNT,
    LAW_HISTORY_LAW_IDS,
    LAW_HISTORY_LAWS,
    LAW_HISTORY_PROFILE,
)
from src.local_profiles import (
    LocalProfileError,
    apply_functional_uat_law_history_runtime_environment,
    artifact_root,
    ensure_functional_uat_law_history_profile,
    functional_uat_law_history_collection_environment,
    shared_model_cache_directory,
    validate_functional_uat_law_history_api_credential,
)

LAW_HISTORY_LOCAL_STATE_VERSION = "functional-law-history/v3"
_ACTIVE_MANIFEST_NAME = "active-law-history-manifest.json"
_PENDING_REFRESH_NAME = "pending-law-history-refresh.json"
_MUTATION_LOCK_NAME = ".law-history-mutation.lock"
_SOURCE_BATCH_MANIFEST_NAME = "source-manifest.json"
_WINERROR_INVALID_PARAMETER = 87
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MST = re.compile(r"^\d{1,32}$")
_VERSION_KEY = re.compile(r"^(?P<mst>\d{1,32})@(?P<effective>\d{8})$")


class LawHistoryError(RuntimeError):
    """A bounded law-history operation cannot safely continue."""


LawApiGet = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class LawHistorySource:
    """Private local inventory for one official law version."""

    law_id: str
    current_mst: str
    current_file: str
    current_sha256: str
    effective_mst: str
    effective_file: str
    effective_sha256: str
    effective_date: date
    article_count: int
    is_repealed: bool
    law_name: str
    hierarchy: str
    ministry: str
    official_uri: str
    collected_at: str
    source_kind: Literal["baseline", "refresh"]

    @property
    def version_key(self) -> str:
        """Immutable internal identity for a phased official MST version.

        An official MST is a promulgation unit, not necessarily one effective
        body: a single MST can have several phased enforcement dates.  Keep
        the public MST separate for citations and derive this local join key
        from the exact official request identity.
        """

        return _law_history_version_key(self.effective_mst, self.effective_date)

    def local_state(self, *, profile: Path, source_root: Path) -> dict[str, object]:
        return {
            "law_id": self.law_id,
            "current_mst": self.current_mst,
            "current_file": str((source_root / self.current_file).relative_to(profile)),
            "current_sha256": self.current_sha256,
            "effective_mst": self.effective_mst,
            "version_key": self.version_key,
            "effective_file": str((source_root / self.effective_file).relative_to(profile)),
            "effective_sha256": self.effective_sha256,
            "effective_date": self.effective_date.isoformat(),
            "article_count": self.article_count,
            "is_repealed": self.is_repealed,
            "law_name": self.law_name,
            "hierarchy": self.hierarchy,
            "ministry": self.ministry,
            "official_uri": self.official_uri,
            "collected_at": self.collected_at,
            "source_kind": self.source_kind,
        }


@dataclass(slots=True)
class _ProjectedSourceArticle:
    """One immutable article interval reconstructed from official bodies.

    This exists only while an explicit full-store verification runs.  It keeps
    the parent pointer as an object so an unchanged child is tied to the exact
    retained parent version, matching ``load_law_history_version`` semantics.
    """

    law_id: str
    article_no: int
    paragraph_no: int | None
    title: str | None
    content_hash: str
    version_mst: str
    version_key: str
    valid_from: date
    valid_to: date | None
    parent: _ProjectedSourceArticle | None = None


@dataclass(frozen=True, slots=True)
class LawHistoryVersionCandidate:
    """One page-verified official eflaw list entry for a fixed law ID."""

    law_id: str
    mst: str
    effective_date: date

    @property
    def version_key(self) -> str:
        return _law_history_version_key(self.mst, self.effective_date)


@dataclass(frozen=True, slots=True)
class _CollectedCatalogCandidate:
    """One exact official body held only until its date group is resolved.

    The raw body is never put in a manifest or a log.  It stays in memory
    only while one co-effective candidate group is normalized, and only the
    verified canonical body is written under the ignored profile directory.
    """

    candidate: LawHistoryVersionCandidate
    source: LawHistorySource
    body: dict[str, Any]
    projection_sha256: str


@dataclass(frozen=True, slots=True)
class FrozenInputAttestation:
    """Exact tracked HSK/CCCT input bytes and their parsed projections."""

    manifest: Mapping[str, object]
    hsk_nodes: list[dict[str, object]]
    ccct_dataset: Any


def _official_uri(mst: str) -> str:
    # This records a public citation target only.  The Law API OC credential
    # never appears in a URI, manifest, log, or returned Evidence payload.
    return f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={mst}"


def _law_history_version_key(mst: str, effective_date: date) -> str:
    """Return the one-to-one local identity for an official MST+시행일.

    A separate canonicalization step resolves a co-effective group only from
    equivalent parsed bodies or a verified official 신구법 predecessor graph.
    The key itself never chooses among different MSTs on the same day.
    """

    if _MST.fullmatch(mst) is None or not isinstance(effective_date, date):
        raise LawHistoryError("LAW_HISTORY_VERSION_KEY_INVALID")
    return f"{mst}@{effective_date:%Y%m%d}"


def _valid_law_history_version_key(value: object, *, mst: str, effective_date: date) -> bool:
    return (
        isinstance(value, str)
        and _VERSION_KEY.fullmatch(value) is not None
        and value == _law_history_version_key(mst, effective_date)
    )


def _law_hierarchy(name: str) -> str:
    if name.endswith("시행령"):
        return "시행령"
    if name.endswith("시행규칙"):
        return "시행규칙"
    return "법률"


def _is_repeal_revision(value: object) -> bool:
    """Accept an empty exact-effective body only for an explicit repeal."""

    return isinstance(value, str) and re.sub(r"\s+", "", value) == "폐지"


def _is_valid_baseline_asset(asset: Mapping[str, object]) -> bool:
    """Check that a baseline source was effective *by* the baseline date.

    A snapshot date is not every law version's enforcement date.  The
    baseline selects the latest official version in force on 2026-08-05, so
    most laws legitimately have an earlier effective date.  A later version
    or a repeal can never stand in for that date.
    """

    if asset.get("is_repealed") is not False:
        return False
    try:
        effective_date = date.fromisoformat(cast(str, asset.get("effective_date")))
    except (TypeError, ValueError):
        return False
    return effective_date <= LAW_HISTORY_BASELINE_AS_OF


def _atomic_bytes(path: Path, data: bytes, *, replace: bool = False) -> None:
    if path.is_symlink() or (path.exists() and not replace):
        raise LawHistoryError("LAW_HISTORY_OUTPUT_EXISTS")
    descriptor: int | None = None
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_OUTPUT_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _write_json(path: Path, value: object, *, replace: bool = False) -> str:
    payload = canonical_json_bytes(value)
    _atomic_bytes(path, payload, replace=replace)
    return sha256_bytes(payload)


def _source_root(profile: Path, *, label: str) -> Path:
    parent = _safe_directory(profile / "data" / "law-history")
    run_id = f"{label}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    target = parent / run_id
    if target.exists() or target.is_symlink():
        raise LawHistoryError("LAW_HISTORY_SOURCE_OUTPUT_EXISTS")
    target.mkdir()
    return target


def _catalog_for_law(*, law_id: str, api_get: LawApiGet) -> tuple[LawHistoryVersionCandidate, ...]:
    """Enumerate every official 시행일 version for one fixed law ID.

    The law-history profile deliberately does not treat ``lsHstInf.regDt`` as
    a 시행일 history: that feed is keyed by *change registration date* and can
    miss a version registered before a later effective date.  The official
    ``eflaw`` list is instead queried by the fixed law ID, across every page
    and status (history/current/future), so the requested date range is
    complete before any body is fetched.
    """

    from src import lawgo

    candidates: dict[tuple[str, date], LawHistoryVersionCandidate] = {}
    # Query each documented status separately instead of assuming a
    # comma-combined parameter has the same semantics at every API revision.
    # Every status is fully paginated and later de-duplicated only by the
    # documented request identity, ``MST + 시행일``.  A single MST can have
    # phased effective bodies, and different MSTs can take effect together.
    for status in ("1", "2", "3"):
        expected_total: int | None = None
        observed_total = 0
        page = 1
        while True:
            payload = api_get(
                "lawSearch.do",
                target="eflaw",
                LID=law_id,
                nw=status,
                sort="efasc",
                display=100,
                page=page,
            )
            search = payload.get("LawSearch")
            if not isinstance(search, Mapping):
                raise LawHistoryError("LAW_HISTORY_CATALOG_PAGE_INVALID")
            rows = lawgo.as_list(search.get("law"))
            if not all(isinstance(row, dict) for row in rows):
                raise LawHistoryError("LAW_HISTORY_CATALOG_PAGE_INVALID")
            raw_total = lawgo.squash(cast(str | None, search.get("totalCnt")))
            if not raw_total or not raw_total.isdigit():
                raise LawHistoryError("LAW_HISTORY_CATALOG_PAGE_INVALID")
            page_total = int(raw_total)
            if expected_total is None:
                expected_total = page_total
            elif expected_total != page_total:
                raise LawHistoryError("LAW_HISTORY_CATALOG_PAGE_INVALID")
            for row in rows:
                if lawgo.squash(row.get("법령ID")) != law_id:
                    raise LawHistoryError("LAW_HISTORY_CATALOG_LAW_ID_MISMATCH")
                mst = lawgo.squash(row.get("법령일련번호"))
                raw_date = lawgo.squash(row.get("시행일자"))
                if _MST.fullmatch(mst) is None or not re.fullmatch(r"\d{8}", raw_date):
                    raise LawHistoryError("LAW_HISTORY_CATALOG_PAGE_INVALID")
                try:
                    effective_date = datetime.strptime(raw_date, "%Y%m%d").date()
                except ValueError as exc:
                    raise LawHistoryError("LAW_HISTORY_CATALOG_PAGE_INVALID") from exc
                candidate = LawHistoryVersionCandidate(law_id, mst, effective_date)
                key = (candidate.mst, candidate.effective_date)
                existing = candidates.get(key)
                if existing is not None and existing != candidate:
                    raise LawHistoryError("LAW_HISTORY_CATALOG_DUPLICATE")
                candidates[key] = candidate
            observed_total += len(rows)
            if observed_total == expected_total:
                break
            if observed_total > expected_total or not rows or page >= 10_000:
                raise LawHistoryError("LAW_HISTORY_CATALOG_PAGE_INCOMPLETE")
            page += 1
    return tuple(sorted(candidates.values(), key=lambda value: (value.effective_date, value.mst)))


def _candidate_group_as_of(
    candidates: Sequence[LawHistoryVersionCandidate], *, as_of: date
) -> tuple[LawHistoryVersionCandidate, ...]:
    """Return only the latest effective-date group eligible at ``as_of``.

    This deliberately does not fetch or resolve unrelated historical groups.
    A co-effective group is normalized only when it is actually needed for a
    baseline or explicit refresh window.
    """

    eligible = [candidate for candidate in candidates if candidate.effective_date <= as_of]
    if not eligible:
        raise LawHistoryError("LAW_HISTORY_CATALOG_COVERAGE_MISSING")
    selected_date = max(candidate.effective_date for candidate in eligible)
    group = tuple(
        sorted(
            (candidate for candidate in eligible if candidate.effective_date == selected_date),
            key=lambda candidate: candidate.version_key,
        )
    )
    if not group or len({candidate.law_id for candidate in group}) != 1:
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
    return group


def _candidate_as_of(
    candidates: Sequence[LawHistoryVersionCandidate], *, as_of: date
) -> LawHistoryVersionCandidate:
    """Compatibility helper for callers that require an unambiguous group."""

    group = _candidate_group_as_of(candidates, as_of=as_of)
    if len(group) != 1:
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_REQUIRED")
    return group[0]


def _parsed_projection_sha256(
    *, meta: Mapping[str, object], rows: Sequence[Mapping[str, object]], is_repealed: bool
) -> str:
    """Hash the legal projection, independent of non-semantic raw JSON shape."""

    law_id = meta.get("law_id")
    law_name = meta.get("law_name")
    ministry = meta.get("ministry")
    effective = meta.get("enforcement_date")
    if (
        not isinstance(law_id, str)
        or not law_id
        or not isinstance(law_name, str)
        or not law_name
        or not isinstance(ministry, str)
        or not isinstance(effective, str)
        or not re.fullmatch(r"\d{8}", effective)
    ):
        raise LawHistoryError("LAW_HISTORY_SOURCE_METADATA_INVALID")

    seen: set[tuple[int, int | None]] = set()

    def project_row(value: Mapping[str, object], *, expected_child: bool) -> dict[str, object]:
        article_no = value.get("article_no")
        paragraph_no = value.get("paragraph_no")
        title = value.get("title")
        valid_from = value.get("valid_from")
        content = value.get("content")
        if (
            type(article_no) is not int
            or (paragraph_no is not None and type(paragraph_no) is not int)
            or (expected_child and paragraph_no is None)
            or (not expected_child and paragraph_no is not None)
            or (title is not None and not isinstance(title, str))
            or not isinstance(valid_from, date)
            or not isinstance(content, str)
        ):
            raise LawHistoryError("LAW_HISTORY_SOURCE_INVALID")
        identity = (article_no, paragraph_no)
        if identity in seen:
            raise LawHistoryError("LAW_HISTORY_SOURCE_INVALID")
        seen.add(identity)
        return {
            "article_no": article_no,
            "paragraph_no": paragraph_no,
            "title": title,
            "valid_from": valid_from.isoformat(),
            "content": content,
        }

    projected: list[dict[str, object]] = []
    for parent in rows:
        parent_projection = project_row(parent, expected_child=False)
        children = parent.get("children")
        if not isinstance(children, list):
            raise LawHistoryError("LAW_HISTORY_SOURCE_INVALID")
        parent_projection["children"] = sorted(
            (
                project_row(cast(Mapping[str, object], child), expected_child=True)
                for child in children
            ),
            key=lambda child: (cast(int, child["article_no"]), cast(int, child["paragraph_no"])),
        )
        projected.append(parent_projection)
    if is_repealed:
        if projected:
            raise LawHistoryError("LAW_HISTORY_REPEAL_BODY_AMBIGUOUS")
    elif not projected:
        raise LawHistoryError("LAW_HISTORY_EFFECTIVE_ARTICLES_MISSING")
    return sha256_bytes(
        canonical_json_bytes(
            {
                "law_id": law_id,
                "law_name": law_name,
                "hierarchy": _law_hierarchy(law_name),
                "ministry": ministry,
                "effective_date": effective,
                "is_repealed": is_repealed,
                "articles": sorted(projected, key=lambda row: cast(int, row["article_no"])),
            }
        )
    )


def _prepare_catalog_candidate(
    *,
    candidate: LawHistoryVersionCandidate,
    source_kind: Literal["baseline", "refresh"],
    api_get: LawApiGet,
) -> _CollectedCatalogCandidate:
    """Fetch and validate one exact body without making it active local state."""

    from src.ingest.laws import build_meta, parse_articles

    body = api_get(
        "lawService.do",
        target="eflaw",
        MST=candidate.mst,
        efYd=candidate.effective_date.strftime("%Y%m%d"),
    )
    meta = build_meta(body, candidate.mst)
    if meta.get("law_id") != candidate.law_id:
        raise LawHistoryError("LAW_HISTORY_SOURCE_ID_MISMATCH")
    try:
        effective_date = datetime.strptime(str(meta["enforcement_date"]), "%Y%m%d").date()
    except (KeyError, TypeError, ValueError) as exc:
        raise LawHistoryError("LAW_HISTORY_EFFECTIVE_DATE_INVALID") from exc
    if effective_date != candidate.effective_date:
        raise LawHistoryError("LAW_HISTORY_EFFECTIVE_DATE_INVALID")
    rows = parse_articles(body, meta)
    article_count = sum(1 + len(row.get("children", [])) for row in rows)
    is_repealed = _is_repeal_revision(meta.get("revision_type"))
    if is_repealed and article_count:
        raise LawHistoryError("LAW_HISTORY_REPEAL_BODY_AMBIGUOUS")
    if not is_repealed and article_count < 1:
        raise LawHistoryError("LAW_HISTORY_EFFECTIVE_ARTICLES_MISSING")
    law_name = meta.get("law_name")
    ministry = meta.get("ministry")
    if not isinstance(law_name, str) or not law_name or not isinstance(ministry, str):
        raise LawHistoryError("LAW_HISTORY_SOURCE_METADATA_INVALID")
    filename = (
        f"law_version_{candidate.law_id}_{candidate.mst}_{candidate.effective_date:%Y%m%d}.json"
    )
    raw_sha256 = sha256_bytes(canonical_json_bytes(body))
    source = LawHistorySource(
        law_id=candidate.law_id,
        current_mst=candidate.mst,
        current_file=filename,
        current_sha256=raw_sha256,
        effective_mst=candidate.mst,
        effective_file=filename,
        effective_sha256=raw_sha256,
        effective_date=effective_date,
        article_count=article_count,
        is_repealed=is_repealed,
        law_name=law_name,
        hierarchy=_law_hierarchy(law_name),
        ministry=ministry,
        official_uri=_official_uri(candidate.mst),
        collected_at=datetime.now(timezone.utc).isoformat(),
        source_kind=source_kind,
    )
    return _CollectedCatalogCandidate(
        candidate=candidate,
        source=source,
        body=body,
        projection_sha256=_parsed_projection_sha256(meta=meta, rows=rows, is_repealed=is_repealed),
    )


def _old_and_new_predecessor(*, candidate: LawHistoryVersionCandidate, api_get: LawApiGet) -> str:
    """Read one official direct predecessor edge without retaining its body."""

    from src import lawgo

    payload = api_get("lawService.do", target="oldAndNew", MST=candidate.mst)
    service = payload.get("OldAndNewService")
    if not isinstance(service, Mapping):
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
    old = service.get("구조문_기본정보")
    new = service.get("신조문_기본정보")
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
    predecessor = lawgo.squash(cast(str | None, old.get("법령일련번호")))
    successor = lawgo.squash(cast(str | None, new.get("법령일련번호")))
    old_law_id = lawgo.squash(cast(str | None, old.get("법령ID")))
    new_law_id = lawgo.squash(cast(str | None, new.get("법령ID")))
    if (
        _MST.fullmatch(predecessor) is None
        or _MST.fullmatch(successor) is None
        or predecessor == successor
        or successor != candidate.mst
        or old_law_id != candidate.law_id
        or new_law_id != candidate.law_id
    ):
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
    return predecessor


def _canonicalize_coeffective_candidates(
    *, receipts: Sequence[_CollectedCatalogCandidate], api_get: LawApiGet
) -> _CollectedCatalogCandidate:
    """Choose one legal state for a same-day group, or fail closed.

    The official list does not document an MST numeric or response-order
    priority.  Equivalent parsed projections are therefore interchangeable
    for legal retrieval and use a deterministic receipt representative.  If
    they differ, only a verified official 구조문→신조문 graph with exactly
    one reachable terminal version may establish the canonical body.
    """

    if not receipts:
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
    law_ids = {receipt.candidate.law_id for receipt in receipts}
    effective_dates = {receipt.candidate.effective_date for receipt in receipts}
    msts = {receipt.candidate.mst for receipt in receipts}
    if (
        len(law_ids) != 1
        or len(effective_dates) != 1
        or len(msts) != len(receipts)
        or any(
            receipt.source.law_id != receipt.candidate.law_id
            or receipt.source.effective_mst != receipt.candidate.mst
            or receipt.source.effective_date != receipt.candidate.effective_date
            or _SHA256.fullmatch(receipt.projection_sha256) is None
            for receipt in receipts
        )
    ):
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
    if len(receipts) == 1:
        return receipts[0]
    if len({receipt.projection_sha256 for receipt in receipts}) == 1:
        # This only chooses a citation representative; the complete parsed
        # legal projection is already equal for every official candidate.
        return min(receipts, key=lambda receipt: receipt.candidate.version_key)

    outgoing: dict[str, set[str]] = {mst: set() for mst in msts}
    for receipt in receipts:
        predecessor = _old_and_new_predecessor(candidate=receipt.candidate, api_get=api_get)
        if predecessor in msts:
            outgoing[predecessor].add(receipt.candidate.mst)
    sinks = [mst for mst, successors in outgoing.items() if not successors]
    if len(sinks) != 1:
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(mst: str) -> None:
        if mst in visiting:
            raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
        if mst in visited:
            return
        visiting.add(mst)
        for successor in outgoing[mst]:
            visit(successor)
        visiting.remove(mst)
        visited.add(mst)

    for mst in msts:
        visit(mst)
    sink = sinks[0]
    reverse: dict[str, set[str]] = {mst: set() for mst in msts}
    for predecessor, successors in outgoing.items():
        for successor in successors:
            reverse[successor].add(predecessor)
    reachable: set[str] = set()
    frontier = [sink]
    while frontier:
        current = frontier.pop()
        if current in reachable:
            continue
        reachable.add(current)
        frontier.extend(reverse[current])
    if reachable != msts:
        raise LawHistoryError("LAW_HISTORY_CATALOG_CANONICAL_UNRESOLVED")
    return next(receipt for receipt in receipts if receipt.candidate.mst == sink)


def _write_collected_catalog_candidate(
    *, source_root: Path, receipt: _CollectedCatalogCandidate
) -> LawHistorySource:
    """Persist only the selected official body under the ignored profile root."""

    actual_sha256 = _write_json(source_root / receipt.source.effective_file, receipt.body)
    if actual_sha256 != receipt.source.effective_sha256:
        raise LawHistoryError("LAW_HISTORY_SOURCE_HASH_MISMATCH")
    return receipt.source


def _collect_canonical_candidate_group(
    *,
    source_root: Path,
    candidates: Sequence[LawHistoryVersionCandidate],
    source_kind: Literal["baseline", "refresh"],
    api_get: LawApiGet,
) -> LawHistorySource:
    """Fetch exactly one needed date group and persist its canonical body."""

    receipts = tuple(
        _prepare_catalog_candidate(candidate=candidate, source_kind=source_kind, api_get=api_get)
        for candidate in candidates
    )
    selected = _canonicalize_coeffective_candidates(receipts=receipts, api_get=api_get)
    return _write_collected_catalog_candidate(source_root=source_root, receipt=selected)


def _collect_catalog_candidate(
    *,
    source_root: Path,
    candidate: LawHistoryVersionCandidate,
    source_kind: Literal["baseline", "refresh"],
    api_get: LawApiGet,
) -> LawHistorySource:
    """Fetch and persist one unambiguous exact eflaw body."""

    return _write_collected_catalog_candidate(
        source_root=source_root,
        receipt=_prepare_catalog_candidate(
            candidate=candidate, source_kind=source_kind, api_get=api_get
        ),
    )


def _write_source_batch_manifest(
    *,
    root: Path,
    range_fields: Mapping[str, str],
    sources: Sequence[LawHistorySource],
) -> None:
    _write_json(
        root / _SOURCE_BATCH_MANIFEST_NAME,
        {
            "local_state_version": LAW_HISTORY_LOCAL_STATE_VERSION,
            "profile": LAW_HISTORY_PROFILE,
            **dict(range_fields),
            "sources": [
                {
                    "law_id": source.law_id,
                    "effective_mst": source.effective_mst,
                    "version_key": source.version_key,
                    "effective_sha256": source.effective_sha256,
                    "effective_date": source.effective_date.isoformat(),
                    "article_count": source.article_count,
                    "is_repealed": source.is_repealed,
                }
                for source in sources
            ],
        },
    )


def collect_law_history_baseline(
    *,
    profile: Path,
    as_of: date,
    api_get: LawApiGet,
) -> tuple[Path, tuple[LawHistorySource, ...]]:
    """Collect all 65 exact official eflaw bodies for the baseline date."""

    if as_of != LAW_HISTORY_BASELINE_AS_OF:
        raise LawHistoryError("LAW_HISTORY_BASELINE_AS_OF_MISMATCH")
    root = _source_root(profile, label="baseline")
    try:
        sources = tuple(
            _collect_canonical_candidate_group(
                source_root=root,
                candidates=_candidate_group_as_of(
                    _catalog_for_law(law_id=law_id, api_get=api_get), as_of=as_of
                ),
                source_kind="baseline",
                api_get=api_get,
            )
            for law_id in LAW_HISTORY_LAW_IDS
        )
        if any(source.is_repealed for source in sources):
            # The fixed registry is explicitly a baseline of current laws.
            # A repeal at the baseline date is a candidate-registry failure,
            # not a silently incomplete 65-law Evidence build.
            raise LawHistoryError("LAW_HISTORY_BASELINE_LAW_REPEALED")
        _write_source_batch_manifest(
            root=root,
            range_fields={"as_of": as_of.isoformat()},
            sources=sources,
        )
        return root, sources
    except Exception:
        # This is a just-created, validated child path.  A failed collection
        # never becomes an active source set.
        shutil.rmtree(root, ignore_errors=True)
        raise


def collect_law_history_refresh(
    *,
    profile: Path,
    from_date: date,
    to_date: date,
    api_get: LawApiGet,
) -> tuple[Path, tuple[LawHistorySource, ...]]:
    """Collect all complete official eflaw versions effective in a range.

    Each law's to-date version is included even when unchanged in the range.
    That makes the refresh prove there is no gap through ``to_date`` rather
    than merely observing the date a legal change happened to be registered.
    """

    if from_date > to_date or from_date < LAW_HISTORY_BASELINE_AS_OF:
        raise LawHistoryError("LAW_HISTORY_REFRESH_RANGE_INVALID")
    root = _source_root(profile, label="refresh")
    try:
        selected: dict[tuple[str, date], tuple[LawHistoryVersionCandidate, ...]] = {}
        for law_id in LAW_HISTORY_LAW_IDS:
            catalog = _catalog_for_law(law_id=law_id, api_get=api_get)
            grouped: dict[date, list[LawHistoryVersionCandidate]] = {}
            for candidate in catalog:
                if from_date <= candidate.effective_date <= to_date:
                    grouped.setdefault(candidate.effective_date, []).append(candidate)
            for effective_date, group in grouped.items():
                selected[(law_id, effective_date)] = tuple(
                    sorted(group, key=lambda candidate: candidate.version_key)
                )
            current_at_to = _candidate_group_as_of(catalog, as_of=to_date)
            selected[(law_id, current_at_to[0].effective_date)] = current_at_to
        sources = tuple(
            _collect_canonical_candidate_group(
                source_root=root,
                candidates=candidates,
                source_kind="refresh",
                api_get=api_get,
            )
            for _key, candidates in sorted(
                selected.items(), key=lambda item: (item[0][1], item[0][0])
            )
        )
        _write_source_batch_manifest(
            root=root,
            range_fields={"from": from_date.isoformat(), "to": to_date.isoformat()},
            sources=sources,
        )
        return root, sources
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _load_source_body(profile: Path, source: Mapping[str, object]) -> tuple[dict[str, Any], Path]:
    relative = source.get("effective_file")
    expected = source.get("effective_sha256")
    if (
        not isinstance(relative, str)
        or not isinstance(expected, str)
        or _SHA256.fullmatch(expected) is None
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    candidate = _profile_source_file(profile, relative)
    if not candidate.is_file() or sha256_bytes(candidate.read_bytes()) != expected:
        raise LawHistoryError("LAW_HISTORY_SOURCE_HASH_MISMATCH")
    try:
        return json.loads(candidate.read_text(encoding="utf-8")), candidate
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryError("LAW_HISTORY_SOURCE_INVALID") from exc


def _profile_source_file(profile: Path, relative: str) -> Path:
    """Resolve a profile-local raw source without accepting path escapes.

    ``pathlib`` treats a Windows drive-relative path such as ``\\outside`` as
    non-absolute on some platforms, then silently discards the profile when
    joined.  Validate the lexical drive/segments, every existing profile
    component, and the resolved containment before a local receipt can be
    opened or hashed.
    """

    value = Path(relative)
    if (
        not relative
        or relative.startswith(("/", "\\"))
        or re.match(r"^[A-Za-z]:", relative) is not None
        or value.is_absolute()
        or value.drive
        or not value.parts
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise LawHistoryError("LAW_HISTORY_SOURCE_PATH_INVALID")
    absolute_profile = Path(os.path.abspath(profile))
    current = Path(absolute_profile.anchor)
    for part in absolute_profile.parts[1:]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise LawHistoryError("LAW_HISTORY_SOURCE_PATH_INVALID")
    if not absolute_profile.is_dir() or absolute_profile.is_symlink():
        raise LawHistoryError("LAW_HISTORY_SOURCE_PATH_INVALID")
    candidate = absolute_profile.joinpath(*value.parts)
    current = absolute_profile
    for part in value.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise LawHistoryError("LAW_HISTORY_SOURCE_PATH_INVALID")
    try:
        candidate.resolve(strict=False).relative_to(absolute_profile.resolve())
    except ValueError as exc:
        raise LawHistoryError("LAW_HISTORY_SOURCE_PATH_INVALID") from exc
    return candidate


def _tracked_input_file(relative: str, expected_sha256: str, expected_size: int) -> Path:
    """Open only one exact non-symlink tracked Data input path."""

    root = Path(__file__).resolve().parents[1]
    value = Path(relative)
    if (
        not relative
        or value.is_absolute()
        or value.drive
        or not value.parts
        or any(part in {"", ".", ".."} for part in value.parts)
    ):
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PATH_INVALID")
    candidate = root.joinpath(*value.parts)
    try:
        candidate.resolve(strict=True).relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PATH_INVALID") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_MISSING")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_MISSING") from exc
    if len(raw) != expected_size or sha256_bytes(raw) != expected_sha256:
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_HASH_MISMATCH")
    return candidate


def _frozen_input_files() -> tuple[tuple[Path, ...], Path]:
    hsk_paths = tuple(
        _tracked_input_file(relative, digest, byte_size)
        for relative, digest, byte_size in LAW_HISTORY_HSK_INPUTS
    )
    ccct_path = _tracked_input_file(*LAW_HISTORY_CCCT_INPUT)
    return hsk_paths, ccct_path


def build_frozen_input_attestation() -> FrozenInputAttestation:
    """Validate fixed baseline bytes and parse the exact objects to project.

    The source files do not themselves prove an official collection timestamp.
    This is deliberately an attestation of the agreed 2026-08-05 *contract
    snapshot* and exact tracked bytes, not a fabricated external provenance
    claim.  The caller receives the parsed values and must project those same
    objects rather than re-read the files.
    """

    from src.ingest import ccct, hsk

    hsk_paths, ccct_path = _frozen_input_files()
    # Use explicit paths after the receipt check so an accidental module-level
    # path change cannot cause the loader to read a different source.
    levels = hsk.load_level_names(hsk_paths[0])
    leaf_attrs = hsk.load_leaf_attrs(hsk_paths[1])
    hsk_nodes = hsk.build_nodes(levels, leaf_attrs)
    hsk_nodes = [dict(node) for node in hsk_nodes]
    leaf_count = sum(1 for node in hsk_nodes if node.get("is_leaf") is True)
    if len(hsk_nodes) != LAW_HISTORY_HSK_NODE_COUNT or leaf_count != LAW_HISTORY_HSK_LEAF_COUNT:
        raise LawHistoryError("LAW_HISTORY_FROZEN_HSK_SEMANTICS_MISMATCH")
    hsk_digest = sha256_bytes(
        canonical_json_bytes(sorted(hsk_nodes, key=lambda node: cast(str, node["code"])))
    )

    # ``build_dataset`` is intentionally passed fully parsed records from the
    # already verified file.  It keeps the existing snapshot-ID algorithm
    # unchanged while eliminating a second source-file read at build time.
    records = list(ccct._iter_progress_from_path(ccct_path))
    ccct_dataset = ccct.build_dataset(records)
    if (
        ccct_dataset.snapshot_id != LAW_HISTORY_CCCT_SNAPSHOT_ID
        or ccct_dataset.content_hash != LAW_HISTORY_CCCT_SNAPSHOT_ID.removeprefix("ccct_")
        or len(ccct_dataset.queries) != LAW_HISTORY_CCCT_QUERY_COUNT
        or len(ccct_dataset.source_records) != LAW_HISTORY_CCCT_RAW_ROW_COUNT
        or len(ccct_dataset.requirements) != LAW_HISTORY_CCCT_REQUIREMENT_COUNT
        or len(ccct_dataset.documents) != LAW_HISTORY_CCCT_DOCUMENT_COUNT
    ):
        raise LawHistoryError("LAW_HISTORY_FROZEN_CCCT_SEMANTICS_MISMATCH")
    ccct_projection = {
        "queries": sorted(
            (row["hsk10"], row["trade_type"], row["query_hash"]) for row in ccct_dataset.queries
        ),
        "source_records": sorted(
            (row["source_record_id"], row["requirement_id"], row["raw_record_hash"])
            for row in ccct_dataset.source_records
        ),
        "requirements": sorted(
            (row["requirement_id"], row["requirement_version_hash"])
            for row in ccct_dataset.requirements
        ),
        "documents": sorted(
            (row["document_code"], row["normalized_name"]) for row in ccct_dataset.documents
        ),
    }
    ccct_digest = sha256_bytes(canonical_json_bytes(ccct_projection))
    manifest: dict[str, object] = {
        "attestation_kind": LAW_HISTORY_FROZEN_INPUTS_VERSION,
        "hsk_snapshot_as_of": LAW_HISTORY_BASELINE_AS_OF.isoformat(),
        "requirements_snapshot_as_of": LAW_HISTORY_BASELINE_AS_OF.isoformat(),
        "hsk": {
            "files": [
                {"relative_path": relative, "sha256": digest, "byte_size": byte_size}
                for relative, digest, byte_size in LAW_HISTORY_HSK_INPUTS
            ],
            "node_count": len(hsk_nodes),
            "leaf_count": leaf_count,
            "projection_sha256": hsk_digest,
        },
        "ccct": {
            "relative_path": LAW_HISTORY_CCCT_INPUT[0],
            "input_sha256": LAW_HISTORY_CCCT_INPUT[1],
            "byte_size": LAW_HISTORY_CCCT_INPUT[2],
            "snapshot_id": ccct_dataset.snapshot_id,
            "content_hash": ccct_dataset.content_hash,
            "query_count": len(ccct_dataset.queries),
            "raw_row_count": len(ccct_dataset.source_records),
            "requirement_count": len(ccct_dataset.requirements),
            "document_count": len(ccct_dataset.documents),
            "projection_sha256": ccct_digest,
        },
    }
    return FrozenInputAttestation(manifest=manifest, hsk_nodes=hsk_nodes, ccct_dataset=ccct_dataset)


def _validate_frozen_inputs_manifest(value: object) -> Mapping[str, object]:
    """Check local-manifest receipts without exposing them on the API wire."""

    if not isinstance(value, Mapping) or set(value) != {
        "attestation_kind",
        "hsk_snapshot_as_of",
        "requirements_snapshot_as_of",
        "hsk",
        "ccct",
    }:
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_MANIFEST_INVALID")
    if (
        value["attestation_kind"] != LAW_HISTORY_FROZEN_INPUTS_VERSION
        or value["hsk_snapshot_as_of"] != LAW_HISTORY_BASELINE_AS_OF.isoformat()
        or value["requirements_snapshot_as_of"] != LAW_HISTORY_BASELINE_AS_OF.isoformat()
    ):
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_MANIFEST_INVALID")
    hsk_value = value["hsk"]
    ccct_value = value["ccct"]
    if not isinstance(hsk_value, Mapping) or not isinstance(ccct_value, Mapping):
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_MANIFEST_INVALID")
    expected_hsk = [
        {"relative_path": relative, "sha256": digest, "byte_size": byte_size}
        for relative, digest, byte_size in LAW_HISTORY_HSK_INPUTS
    ]
    if (
        hsk_value.get("files") != expected_hsk
        or hsk_value.get("node_count") != LAW_HISTORY_HSK_NODE_COUNT
        or hsk_value.get("leaf_count") != LAW_HISTORY_HSK_LEAF_COUNT
        or not isinstance(hsk_value.get("projection_sha256"), str)
        or _SHA256.fullmatch(cast(str, hsk_value.get("projection_sha256"))) is None
    ):
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_MANIFEST_INVALID")
    if (
        ccct_value.get("relative_path") != LAW_HISTORY_CCCT_INPUT[0]
        or ccct_value.get("input_sha256") != LAW_HISTORY_CCCT_INPUT[1]
        or ccct_value.get("byte_size") != LAW_HISTORY_CCCT_INPUT[2]
        or ccct_value.get("snapshot_id") != LAW_HISTORY_CCCT_SNAPSHOT_ID
        or ccct_value.get("content_hash") != LAW_HISTORY_CCCT_SNAPSHOT_ID.removeprefix("ccct_")
        or ccct_value.get("query_count") != LAW_HISTORY_CCCT_QUERY_COUNT
        or ccct_value.get("raw_row_count") != LAW_HISTORY_CCCT_RAW_ROW_COUNT
        or ccct_value.get("requirement_count") != LAW_HISTORY_CCCT_REQUIREMENT_COUNT
        or ccct_value.get("document_count") != LAW_HISTORY_CCCT_DOCUMENT_COUNT
        or not isinstance(ccct_value.get("projection_sha256"), str)
        or _SHA256.fullmatch(cast(str, ccct_value.get("projection_sha256"))) is None
    ):
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_MANIFEST_INVALID")
    _frozen_input_files()
    return value


def _is_rebindable_stale_frozen_inputs(*, stale: object, current: Mapping[str, object]) -> bool:
    """Allow one narrowly-defined fixed-input receipt migration.

    A published law-history marker normally cannot be altered unless an
    explicit refresh or reviewed benchmark changes it.  One exceptional
    maintenance case is a correction to the *tracked-byte receipt* for the
    fixed CCCT input: the source/store projection remains identical, while
    the persisted receipt no longer matches the canonical checkout bytes.

    This predicate deliberately accepts only that case.  HSK receipts and all
    semantic/projection fields must already equal the freshly attested fixed
    inputs; the old CCCT digest and byte count merely have to be well-formed
    and differ from the current pair.  It is not a generic malformed-manifest
    repair path.
    """

    try:
        verified_current = _validate_frozen_inputs_manifest(current)
    except LawHistoryError:
        return False
    if not isinstance(stale, Mapping) or set(stale) != set(verified_current):
        return False
    if any(
        stale.get(key) != verified_current.get(key)
        for key in (
            "attestation_kind",
            "hsk_snapshot_as_of",
            "requirements_snapshot_as_of",
        )
    ):
        return False
    stale_hsk = stale.get("hsk")
    current_hsk = verified_current.get("hsk")
    stale_ccct = stale.get("ccct")
    current_ccct = verified_current.get("ccct")
    if (
        not isinstance(stale_hsk, Mapping)
        or not isinstance(current_hsk, Mapping)
        or stale_hsk != current_hsk
        or not isinstance(stale_ccct, Mapping)
        or not isinstance(current_ccct, Mapping)
        or set(stale_ccct) != set(current_ccct)
    ):
        return False
    if any(
        stale_ccct.get(key) != current_ccct.get(key)
        for key in set(current_ccct) - {"input_sha256", "byte_size"}
    ):
        return False
    stale_digest = stale_ccct.get("input_sha256")
    stale_size = stale_ccct.get("byte_size")
    if (
        not isinstance(stale_digest, str)
        or _SHA256.fullmatch(stale_digest) is None
        or type(stale_size) is not int
        or stale_size < 1
    ):
        return False
    return stale_digest != current_ccct.get("input_sha256") or stale_size != current_ccct.get(
        "byte_size"
    )


def _validate_frozen_input_projections(attestation: Mapping[str, object]) -> None:
    """Lightweight runtime proof that the attested fixed inputs are projected."""

    from src.db.neo4j import driver as neo4j_driver
    from src.db.postgres import connect

    ccct_value = cast(Mapping[str, object], attestation["ccct"])
    snapshot_id = cast(str, ccct_value["snapshot_id"])
    content_hash = cast(str, ccct_value["content_hash"])
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT snapshot_id, content_hash, query_count, raw_row_count
                FROM knowledge_snapshots
                WHERE source_type='ccct' AND is_current
                """
            )
            rows = cur.fetchall()
            if rows != [
                (
                    snapshot_id,
                    content_hash,
                    LAW_HISTORY_CCCT_QUERY_COUNT,
                    LAW_HISTORY_CCCT_RAW_ROW_COUNT,
                )
            ]:
                raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PROJECTION_MISMATCH")
            for table, expected in (
                ("ccct_queries", LAW_HISTORY_CCCT_QUERY_COUNT),
                ("ccct_source_records", LAW_HISTORY_CCCT_RAW_ROW_COUNT),
                ("ccct_requirements", LAW_HISTORY_CCCT_REQUIREMENT_COUNT),
            ):
                cur.execute(f"SELECT count(*) FROM {table} WHERE snapshot_id=%s", (snapshot_id,))
                row = cur.fetchone()
                if not isinstance(row, tuple) or row != (expected,):
                    raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PROJECTION_MISMATCH")
        driver = neo4j_driver()
        try:
            with driver.session() as session:
                hsk_row = session.run(
                    """
                    MATCH (n:HSNode)
                    RETURN count(n) AS node_count,
                           count(CASE WHEN n:HSK THEN 1 END) AS leaf_count
                    """
                ).single()
                req_row = session.run(
                    """
                    MATCH (r:Requirement {source_type: 'ccct'})
                    RETURN count(r) AS count,
                           collect(DISTINCT r.snapshot_id) AS snapshot_ids
                    """
                ).single()
                if (
                    int(hsk_row["node_count"]) != LAW_HISTORY_HSK_NODE_COUNT
                    or int(hsk_row["leaf_count"]) != LAW_HISTORY_HSK_LEAF_COUNT
                    or int(req_row["count"]) != LAW_HISTORY_CCCT_REQUIREMENT_COUNT
                    or set(req_row["snapshot_ids"]) != {snapshot_id}
                ):
                    raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PROJECTION_MISMATCH")
        finally:
            driver.close()
    except LawHistoryError:
        raise
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PROJECTION_UNAVAILABLE") from exc


def _neo4j_property_projection(node: Mapping[str, object]) -> dict[str, object]:
    """Return the exact property map Neo4j can persist for an HSK node.

    Neo4j properties cannot retain ``null`` values: ``SET n = row`` removes a
    property whose driver value is ``None``.  The frozen HSK input deliberately
    retains those keys so its source semantics and digest remain exact, while
    the store-projection comparison must use the corresponding persisted map.
    Only expected input is normalized; an unexpected stored key or any
    non-null field/value change still makes the comparison fail closed.
    """

    return {key: value for key, value in node.items() if value is not None}


def _verify_frozen_input_projections_full(attestation: FrozenInputAttestation) -> None:
    """Explicitly compare every fixed HSK/CCCT projection with its inputs."""

    from src.db.neo4j import driver as neo4j_driver
    from src.db.postgres import connect

    manifest = _validate_frozen_inputs_manifest(attestation.manifest)
    ccct_value = cast(Mapping[str, object], manifest["ccct"])
    snapshot_id = cast(str, ccct_value["snapshot_id"])
    dataset = attestation.ccct_dataset

    expected_queries = {
        (
            str(row["hsk10"]),
            str(row["trade_type"]),
            str(row["source_uri"]),
            int(row["item_count"]),
            str(row["query_hash"]),
        )
        for row in dataset.queries
    }
    expected_sources = {
        (
            str(row["source_record_id"]),
            str(row["requirement_id"]),
            str(row["hsk10"]),
            str(row["trade_type"]),
            int(row["source_item_index"]),
            str(row["source_uri"]),
            str(row["raw_record_hash"]),
        )
        for row in dataset.source_records
    }
    expected_requirements = {
        (
            str(row["requirement_id"]),
            str(row["requirement_version_hash"]),
            str(row["hsk10"]),
            str(row["trade_type"]),
            str(row["law_code"]),
            str(row["law_name"]),
            str(row["agency_code"]),
            str(row["agency_name"]),
            str(row["document_code"]),
            str(row["document_name"]),
            str(row["valid_from"]) if row["valid_from"] is not None else None,
            str(row["valid_to"]) if row["valid_to"] is not None else None,
            str(row["rtm_tpcd"]),
        )
        for row in dataset.requirements
    }
    expected_documents = {
        (str(row["document_code"]), str(row["display_name"]), str(row["normalized_name"]))
        for row in dataset.documents
    }
    expected_hsk = {
        (canonical_json_bytes(_neo4j_property_projection(node)), bool(node.get("is_leaf")))
        for node in attestation.hsk_nodes
    }
    expected_graph = {
        (
            str(row["requirement_id"]),
            str(row["hsk10"]),
            str(row["law_code"]),
            str(row["agency_code"]),
            str(row["document_code"]),
            str(row["requirement_version_hash"]),
        )
        for row in dataset.requirements
    }
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT hsk10, trade_type, source_uri, item_count, query_hash
                FROM ccct_queries WHERE snapshot_id=%s
                """,
                (snapshot_id,),
            )
            actual_queries = {
                (str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4]))
                for row in cur.fetchall()
            }
            cur.execute(
                """
                SELECT source_record_id, requirement_id, hsk10, trade_type,
                       source_item_index, source_uri, raw_record_hash
                FROM ccct_source_records WHERE snapshot_id=%s
                """,
                (snapshot_id,),
            )
            actual_sources = {
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    int(row[4]),
                    str(row[5]),
                    str(row[6]),
                )
                for row in cur.fetchall()
            }
            cur.execute(
                """
                SELECT requirement_id, requirement_version_hash, hsk10, trade_type,
                       law_code, law_name, agency_code, agency_name, document_code,
                       document_name, valid_from, valid_to, rtm_tpcd
                FROM ccct_requirements WHERE snapshot_id=%s
                """,
                (snapshot_id,),
            )
            actual_requirements = {
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                    str(row[5]),
                    str(row[6]),
                    str(row[7]),
                    str(row[8]),
                    str(row[9]),
                    str(row[10]) if row[10] is not None else None,
                    str(row[11]) if row[11] is not None else None,
                    str(row[12]),
                )
                for row in cur.fetchall()
            }
            cur.execute(
                "SELECT document_code, display_name, normalized_name FROM ccct_requirement_documents"
            )
            actual_documents = {(str(row[0]), str(row[1]), str(row[2])) for row in cur.fetchall()}
        driver = neo4j_driver()
        try:
            with driver.session() as session:
                hsk_rows = session.run(
                    "MATCH (n:HSNode) RETURN properties(n) AS props, n:HSK AS is_hsk"
                )
                actual_hsk = {
                    (canonical_json_bytes(dict(row["props"])), bool(row["is_hsk"]))
                    for row in hsk_rows
                }
                graph_rows = session.run(
                    """
                    MATCH (h:HSK)-[:HAS_REQUIREMENT]->(r:Requirement {source_type: 'ccct'})
                    MATCH (r)-[:BASED_ON]->(law:Law)
                    MATCH (r)-[:APPROVED_BY]->(agency:Agency)
                    MATCH (r)-[:REQUIRES_DOCUMENT]->(document:Certificate)
                    RETURN r.id AS requirement_id, h.code AS hsk10, law.code AS law_code,
                           agency.code AS agency_code, document.code AS document_code,
                           r.version_hash AS version_hash
                    """
                )
                actual_graph = {
                    (
                        str(row["requirement_id"]),
                        str(row["hsk10"]),
                        str(row["law_code"]),
                        str(row["agency_code"]),
                        str(row["document_code"]),
                        str(row["version_hash"]),
                    )
                    for row in graph_rows
                }
        finally:
            driver.close()
    except LawHistoryError:
        raise
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PROJECTION_UNAVAILABLE") from exc
    if (
        actual_queries != expected_queries
        or actual_sources != expected_sources
        or actual_requirements != expected_requirements
        or actual_documents != expected_documents
        or actual_hsk != expected_hsk
        or actual_graph != expected_graph
    ):
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PROJECTION_MISMATCH")


def _record_source_provenance(cur, source: LawHistorySource) -> None:
    cur.execute(
        "SELECT official_uri, raw_sha256, collected_at, effective_date, article_count, "
        "is_repealed, source_kind "
        "FROM law_version_sources "
        "WHERE law_id=%s AND version_key=%s",
        (source.law_id, source.version_key),
    )
    existing = cur.fetchone()
    if existing is not None:
        collected_at = datetime.fromisoformat(source.collected_at)
        if (
            str(existing[0]) != source.official_uri
            or str(existing[1]) != source.effective_sha256
            or existing[2] != collected_at
            or existing[3] != source.effective_date
            or int(existing[4]) != source.article_count
            or bool(existing[5]) != source.is_repealed
            or str(existing[6]) != source.source_kind
        ):
            raise LawHistoryError("LAW_HISTORY_SOURCE_PROVENANCE_MISMATCH")
        return
    cur.execute(
        """
        INSERT INTO law_version_sources
          (law_id, mst, version_key, official_uri, raw_sha256, collected_at, effective_date,
           article_count, is_repealed, source_kind)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            source.law_id,
            source.effective_mst,
            source.version_key,
            source.official_uri,
            source.effective_sha256,
            datetime.fromisoformat(source.collected_at),
            source.effective_date,
            source.article_count,
            source.is_repealed,
            source.source_kind,
        ),
    )


def _history_store_counts() -> dict[str, int]:
    from src.db.neo4j import driver as neo4j_driver
    from src.db.postgres import connect
    from src.db.qdrant import TEMPORAL_COLLECTION, client as qdrant_client

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(DISTINCT law_id), count(*) FROM law_version_sources")
            law_count, source_count = (int(value) for value in cur.fetchone())
            cur.execute(
                """
                SELECT count(*)
                FROM law_articles a
                JOIN law_version_sources s
                  ON s.law_id=a.law_id AND s.version_key=a.version_key
                WHERE NOT EXISTS (SELECT 1 FROM law_articles c WHERE c.parent_article_pk=a.id)
                """
            )
            article_count = int(cur.fetchone()[0])
        qc = qdrant_client()
        vector_count = (
            int(qc.count(TEMPORAL_COLLECTION, exact=True).count)
            if qc.collection_exists(TEMPORAL_COLLECTION)
            else 0
        )
        driver = neo4j_driver()
        try:
            with driver.session() as session:
                requirement_count = int(
                    session.run(
                        "MATCH (r:Requirement {source_type: 'ccct'}) RETURN count(r) AS count"
                    ).single()["count"]
                )
        finally:
            driver.close()
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_STORE_COUNT_UNAVAILABLE") from exc
    if law_count != len(LAW_HISTORY_LAW_IDS) or not source_count or not article_count:
        raise LawHistoryError("LAW_HISTORY_STORE_COUNT_MISMATCH")
    if vector_count != article_count or not requirement_count:
        raise LawHistoryError("LAW_HISTORY_STORE_COUNT_MISMATCH")
    return {
        "law_count": law_count,
        "law_version_count": source_count,
        "postgresql_temporal": article_count,
        "qdrant_temporal": vector_count,
        "neo4j_requirements": requirement_count,
    }


def _scroll_temporal_points(*, client: Any, expected_count: int) -> tuple[Any, ...]:
    """Read the isolated temporal collection once with bounded pagination."""

    from src.db.qdrant import TEMPORAL_COLLECTION

    if expected_count < 0:
        raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_INVALID")
    points: list[Any] = []
    offset: object | None = None
    seen_offsets: set[str] = set()
    for _page in range(expected_count + 1):
        page, next_offset = client.scroll(
            collection_name=TEMPORAL_COLLECTION,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if len(page) > 256:
            raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_INVALID")
        points.extend(page)
        if len(points) > expected_count:
            raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
        if next_offset is None:
            if len(points) != expected_count:
                raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
            return tuple(points)
        marker = repr(next_offset)
        if marker in seen_offsets:
            raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_INVALID")
        seen_offsets.add(marker)
        offset = next_offset
    raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_INVALID")


def _verify_temporal_store_projection() -> str:
    """Compare every temporal vector payload with its PostgreSQL article.

    Count equality alone cannot distinguish a stale or swapped vector from
    the expected law version.  The verifier also re-hashes PostgreSQL text
    and compares the Qdrant text/payload fields, so a changed body or citation
    identity cannot remain hidden behind a stale ``content_hash`` column.
    This is deliberately an explicit maintenance verification (and a
    build/refresh publish gate), not a per-request scan.
    """

    from src.db.postgres import connect
    from src.db.qdrant import TEMPORAL_COLLECTION, client as qdrant_client
    from src.ingest.laws import temporal_point_id

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.id, a.law_id, version.law_name, version.hierarchy,
                       a.article_no, a.paragraph_no, a.item_no, a.content,
                       a.version_mst, a.version_key, a.content_hash, a.valid_from, a.valid_to
                FROM law_articles a
                JOIN law_versions version
                  ON version.law_id=a.law_id AND version.version_key=a.version_key
                JOIN law_version_sources source
                  ON source.law_id=a.law_id AND source.version_key=a.version_key
                WHERE NOT EXISTS (
                  SELECT 1 FROM law_articles child WHERE child.parent_article_pk=a.id
                )
                ORDER BY a.id
                """
            )
            rows = cur.fetchall()
        expected: dict[str, dict[str, object]] = {}
        for row in rows:
            if not isinstance(row, tuple) or len(row) != 13:
                raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_INVALID")
            (
                article_pk,
                law_id,
                law_name,
                hierarchy,
                article_no,
                paragraph_no,
                item_no,
                content,
                version_mst,
                version_key,
                content_hash,
                valid_from,
                valid_to,
            ) = row
            if (
                type(article_pk) is not int
                or not isinstance(law_id, str)
                or law_id not in LAW_HISTORY_LAW_IDS
                or not isinstance(law_name, str)
                or not law_name
                or not isinstance(hierarchy, str)
                or hierarchy != _law_hierarchy(law_name)
                or type(article_no) is not int
                or (paragraph_no is not None and type(paragraph_no) is not int)
                or item_no is not None
                or not isinstance(content, str)
                or not isinstance(version_mst, str)
                or _MST.fullmatch(version_mst) is None
                or not isinstance(version_key, str)
                or (version_match := _VERSION_KEY.fullmatch(version_key)) is None
                or version_match.group("mst") != version_mst
                or not isinstance(content_hash, str)
                or _SHA256.fullmatch(content_hash) is None
                or sha256_bytes(content.encode("utf-8")) != content_hash
                or not isinstance(valid_from, date)
                or (valid_to is not None and not isinstance(valid_to, date))
            ):
                raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_INVALID")
            point_id = temporal_point_id(article_pk, version_key)
            if point_id in expected:
                raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_INVALID")
            expected[point_id] = {
                "article_pk": article_pk,
                "law_id": law_id,
                "law_name": law_name,
                "hierarchy": hierarchy,
                "article_no": article_no,
                "paragraph_no": paragraph_no,
                "item_no": item_no,
                "version_mst": version_mst,
                "version_key": version_key,
                "content_hash": content_hash,
                "valid_from_ordinal": valid_from.toordinal(),
                "valid_to_ordinal": (
                    valid_to.toordinal() if valid_to is not None else date.max.toordinal()
                ),
                "text": content,
            }
        if not expected:
            raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
        client = qdrant_client()
        if not client.collection_exists(TEMPORAL_COLLECTION):
            raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
        count = int(client.count(TEMPORAL_COLLECTION, exact=True).count)
        points = _scroll_temporal_points(client=client, expected_count=count)
    except LawHistoryError:
        raise
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_UNAVAILABLE") from exc

    actual: dict[str, Mapping[str, object]] = {}
    for point in points:
        point_id = str(point.id)
        payload = point.payload
        if point_id in actual or not isinstance(payload, Mapping):
            raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
        actual[point_id] = payload
    if set(actual) != set(expected):
        raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
    for point_id, expected_payload in expected.items():
        payload = actual[point_id]
        if any(payload.get(key) != value for key, value in expected_payload.items()):
            raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
    # The manifest binds stable identity/hash/interval fields, not raw text.
    # Text is nevertheless compared above to the database body and therefore
    # transitively to the source-article projection verifier.
    return sha256_bytes(
        canonical_json_bytes(
            [
                {
                    "point_id": point_id,
                    **{key: value for key, value in expected[point_id].items() if key != "text"},
                }
                for point_id in sorted(expected)
            ]
        )
    )


def _law_history_model_inventory(
    *, profile: Path, local_root_override: Path | None = None
) -> dict[str, dict[str, str]]:
    """Bind dense, reranker, and the independent sparse encoder revision."""

    from src.embed.bm25 import ENCODER_VERSION

    models = _verify_cached_models(
        profile,
        cache_directory=shared_model_cache_directory(root=local_root_override),
        include_reranker=False,
    )
    return {**models, "bm25": {"encoder_version": ENCODER_VERSION}}


def _assert_empty_law_history_stores(profile: Path) -> None:
    """Do not merge an untracked build into the isolated history profile."""

    from src.db.neo4j import driver as neo4j_driver
    from src.db.postgres import connect
    from src.db.qdrant import TEMPORAL_COLLECTION, client as qdrant_client

    if _active_manifest_path(profile).exists():
        raise LawHistoryError("LAW_HISTORY_ACTIVE_MANIFEST_EXISTS")
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM laws), "
                "(SELECT count(*) FROM law_articles), "
                "(SELECT count(*) FROM law_version_sources), "
                "(SELECT count(*) FROM knowledge_snapshots)"
            )
            postgres_counts = tuple(int(value) for value in cur.fetchone())
        qc = qdrant_client()
        temporal_count = (
            int(qc.count(TEMPORAL_COLLECTION, exact=True).count)
            if qc.collection_exists(TEMPORAL_COLLECTION)
            else 0
        )
        driver = neo4j_driver()
        try:
            with driver.session() as session:
                graph_count = int(
                    session.run(
                        """
                        MATCH (n)
                        WHERE n:HSNode OR n:HSK OR n:Requirement OR n:Law
                           OR n:Agency OR n:Certificate
                        RETURN count(n) AS count
                        """
                    ).single()["count"]
                )
        finally:
            driver.close()
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_STORES_UNAVAILABLE") from exc
    if any(postgres_counts) or temporal_count or graph_count:
        raise LawHistoryError("LAW_HISTORY_PROFILE_DIRTY")


def _load_hsk_requirements_and_temporal_index(attestation: FrozenInputAttestation) -> None:
    from src.ingest import ccct, graph, hsk
    from src.ingest.laws import index_temporal_qdrant

    hsk.main(nodes=attestation.hsk_nodes)
    ccct.load(dataset=attestation.ccct_dataset)
    index_temporal_qdrant()
    graph.build()


def _new_public_artifact(
    *, as_of: date, counts: Mapping[str, int], models: Mapping[str, object]
) -> Path:
    root = _safe_directory(artifact_root() / "functional-uat-law-history")
    destination = root / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    destination.mkdir()
    path = destination / "manifest.json"
    _write_json(
        path,
        {
            "profile": LAW_HISTORY_PROFILE,
            "law_as_of": as_of.isoformat(),
            "hsk_snapshot_as_of": LAW_HISTORY_BASELINE_AS_OF.isoformat(),
            "requirements_snapshot_as_of": LAW_HISTORY_BASELINE_AS_OF.isoformat(),
            "law_count": len(LAW_HISTORY_LAW_IDS),
            "store_counts": dict(counts),
            "model_ids/revisions": models,
        },
    )
    return path


def _active_manifest_path(profile: Path) -> Path:
    state = _safe_directory(profile / "state")
    path = state / _ACTIVE_MANIFEST_NAME
    if path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_MANIFEST_PATH_UNSAFE")
    return path


def _pending_refresh_path(profile: Path) -> Path:
    path = _safe_directory(profile / "state") / _PENDING_REFRESH_NAME
    if path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_UNSAFE")
    return path


def _mutation_lock_path(profile: Path) -> Path:
    path = _safe_directory(profile / "state") / _MUTATION_LOCK_NAME
    if path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
    return path


@contextmanager
def _law_history_mutation_lock(profile: Path) -> Iterator[None]:
    """Serialize local manifest/store writers without stale-lock cleanup.

    A lock left by an interrupted process remains a deliberate fail-closed
    condition.  No build, refresh, or benchmark apply may overwrite an
    active marker while another explicit mutation could be publishing it.
    """

    lock = _mutation_lock_path(profile)
    descriptor: int | None = None
    try:
        descriptor = os.open(str(lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(
            descriptor,
            canonical_json_bytes(
                {
                    "profile": LAW_HISTORY_PROFILE,
                    "pid": os.getpid(),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ),
        )
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCKED") from exc
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNAVAILABLE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        yield
    finally:
        try:
            if lock.is_symlink() or not lock.is_file():
                raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
            lock.unlink()
        except LawHistoryError:
            raise
        except OSError as exc:
            raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNAVAILABLE") from exc


def _stale_mutation_lock_owner(profile: Path) -> int:
    """Read a lock owner conservatively; malformed locks are never removed."""

    lock = _mutation_lock_path(profile)
    if not lock.is_file() or lock.is_symlink():
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
    try:
        raw = lock.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE") from exc
    if not isinstance(value, dict) or set(value) != {"profile", "pid", "created_at"}:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
    pid = value["pid"]
    created_at = value["created_at"]
    if (
        value["profile"] != LAW_HISTORY_PROFILE
        or type(pid) is not int
        or pid < 1
        or not isinstance(created_at, str)
    ):
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE") from exc
    if created.tzinfo is None or created.utcoffset() is None:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
    return pid


def _mutation_owner_is_live(pid: int) -> bool:
    """Treat an unreadable owner state as live rather than deleting a lock."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # On Windows, ``os.kill(pid, 0)`` maps a missing process to the
        # generic ``OSError`` ``ERROR_INVALID_PARAMETER`` instead of
        # ``ProcessLookupError``.  Only that documented missing-PID result is
        # safe to recover; access-denied and every other unreadable state stay
        # fail-closed as live owners.
        if os.name == "nt" and getattr(exc, "winerror", None) == _WINERROR_INVALID_PARAMETER:
            return False
        return True
    return True


def recover_law_history_refresh_lock(
    *,
    from_date: date,
    to_date: date,
    confirm_profile: str,
    local_root_override: Path | None = None,
) -> bool:
    """Release a crash-left refresh lock after receipt/digest verification.

    This is intentionally an operator-only recovery action, not stale-lock
    cleanup. It does no official collection and never removes a lock for a
    live or unverifiable process. A caller must still run the same explicit
    ``refresh --from/--to`` afterward; that command takes a fresh lock and
    resumes the staged local source receipts idempotently.
    """

    if confirm_profile != LAW_HISTORY_PROFILE:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_CONFIRMATION_INVALID")
    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    lock = _mutation_lock_path(profile)
    if not lock.exists():
        return False
    owner_pid = _stale_mutation_lock_owner(profile)
    if _mutation_owner_is_live(owner_pid):
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_ACTIVE")
    journal = _load_pending_refresh(profile)
    if journal is None:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    previous = journal.get("previous_manifest_sha256")
    if not isinstance(previous, str) or _SHA256.fullmatch(previous) is None:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    staged_assets, _sources, refresh = _journal_refresh_sources(
        profile=profile,
        journal=journal,
        from_date=from_date,
        to_date=to_date,
    )
    active_manifest, active_digest = _load_active_manifest(profile)
    if journal.get("phase") == "staged":
        allowed_digests = {previous}
    elif journal.get("phase") == "ready":
        updated = journal.get("updated_manifest")
        if not isinstance(updated, dict):
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
        updated_digest = sha256_bytes(canonical_json_bytes(updated))
        if active_digest == previous:
            _validate_ready_refresh_manifest_binding(
                profile=profile,
                previous_manifest=active_manifest,
                staged_assets=staged_assets,
                refresh=refresh,
                ready_manifest=updated,
            )
        elif active_digest == updated_digest:
            if active_manifest != updated:
                raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        else:
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        allowed_digests = {previous, updated_digest}
    else:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    if active_digest not in allowed_digests:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    # Re-check directly before unlinking so a replacement/reparse point never
    # turns recovery into an arbitrary local file deletion.
    if lock.is_symlink() or not lock.is_file() or _stale_mutation_lock_owner(profile) != owner_pid:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
    try:
        lock.unlink()
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNAVAILABLE") from exc
    return True


def recover_law_history_build_lock(
    *,
    confirm_profile: str,
    local_root_override: Path | None = None,
) -> bool:
    """Release a verified dead build lock so an operator can reset and rebuild.

    A baseline build has no refresh journal to resume.  This narrowly scoped
    recovery therefore permits a lock release only when no active manifest and
    no refresh journal exist.  It never resets stores itself: the operator must
    explicitly run ``reset`` before the next baseline build, which prevents a
    partly populated store from being silently reused.
    """

    if confirm_profile != LAW_HISTORY_PROFILE:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_CONFIRMATION_INVALID")
    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    lock = _mutation_lock_path(profile)
    if not lock.exists():
        return False
    owner_pid = _stale_mutation_lock_owner(profile)
    if _mutation_owner_is_live(owner_pid):
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_ACTIVE")
    journal = _pending_refresh_path(profile)
    if journal.exists() or journal.is_symlink():
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    active = _active_manifest_path(profile)
    if active.exists():
        if not active.is_file():
            raise LawHistoryError("LAW_HISTORY_MANIFEST_PATH_UNSAFE")
        raise LawHistoryError("LAW_HISTORY_BUILD_RECOVERY_ACTIVE_MANIFEST")
    # Re-check directly before unlinking so a replacement/reparse point never
    # turns recovery into an arbitrary local file deletion.
    if lock.is_symlink() or not lock.is_file() or _stale_mutation_lock_owner(profile) != owner_pid:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
    try:
        lock.unlink()
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNAVAILABLE") from exc
    return True


def recover_law_history_published_lock(
    *,
    confirm_profile: str,
    local_root_override: Path | None = None,
) -> bool:
    """Release a dead lock only after a fully verified manifest publication.

    A process can die after atomically publishing a baseline, refresh, or
    benchmark strategy marker but before its lock context removes the lock.
    That state is not a failed build and must not be reset blindly.  This
    operator action accepts it only when the profile has no pending refresh,
    the lock owner is dead, and a complete PG↔Qdrant/source verification
    proves the active manifest is stable.  It performs no collection or
    mutation beyond removing that verified stale lock.
    """

    if confirm_profile != LAW_HISTORY_PROFILE:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_CONFIRMATION_INVALID")
    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    lock = _mutation_lock_path(profile)
    if not lock.exists():
        return False
    owner_pid = _stale_mutation_lock_owner(profile)
    if _mutation_owner_is_live(owner_pid):
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_ACTIVE")
    journal = _pending_refresh_path(profile)
    if journal.exists() or journal.is_symlink():
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    active = _active_manifest_path(profile)
    if not active.exists() or not active.is_file():
        raise LawHistoryError("LAW_HISTORY_PUBLISHED_RECOVERY_MANIFEST_UNAVAILABLE")

    # Full verification is intentionally stronger than request-time
    # readiness: a crash after publication can only be recovered if all
    # profile-local source, model, PG and temporal-Qdrant invariants agree.
    _manifest, first_digest = validate_law_history_state(local_root_override=local_root_override)
    verified = verify_law_history_store(local_root_override=local_root_override)
    verified_digest = verified.get("manifest_sha256")
    if not isinstance(verified_digest, str) or verified_digest != first_digest:
        raise LawHistoryError("LAW_HISTORY_PUBLISHED_RECOVERY_STATE_INVALID")
    _manifest, final_digest = validate_law_history_state(local_root_override=local_root_override)
    if final_digest != first_digest:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_CONCURRENT_MODIFICATION")
    # Re-check directly before unlinking so a replacement/reparse point never
    # turns recovery into an arbitrary local file deletion.
    if lock.is_symlink() or not lock.is_file() or _stale_mutation_lock_owner(profile) != owner_pid:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNSAFE")
    try:
        lock.unlink()
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_MUTATION_LOCK_UNAVAILABLE") from exc
    return True


def _source_receipt_rows(sources: Sequence[LawHistorySource]) -> list[dict[str, object]]:
    """Compact, deterministic receipt identity for one explicit refresh."""

    rows = [
        {
            "law_id": source.law_id,
            "mst": source.effective_mst,
            "version_key": source.version_key,
            "effective_date": source.effective_date.isoformat(),
            "raw_sha256": source.effective_sha256,
            "is_repealed": source.is_repealed,
        }
        for source in sources
    ]
    if len({(row["law_id"], row["version_key"]) for row in rows}) != len(rows):
        raise LawHistoryError("LAW_HISTORY_REFRESH_RECEIPT_INVALID")
    return sorted(
        rows,
        key=lambda row: (row["effective_date"], row["law_id"], row["version_key"]),
    )


def _as_of_source_rows(
    sources: Sequence[LawHistorySource], *, to_date: date
) -> list[dict[str, object]]:
    """Bind each refresh to its one latest collected version per law.

    A refresh may include several versions for one law within the requested
    date range. Its coverage claim nevertheless needs an explicit receipt for
    the version selected at the range end. The returned rows have the same
    compact identity as normal source receipts and are ordered by the reviewed
    65-law registry rather than API response order.
    """

    selected: dict[str, LawHistorySource] = {}
    for source in sources:
        if source.effective_date > to_date:
            raise LawHistoryError("LAW_HISTORY_REFRESH_RECEIPT_INVALID")
        previous = selected.get(source.law_id)
        if previous is not None and source.effective_date == previous.effective_date:
            # A request range must already have a single canonical official
            # body for each law/day.  Never turn unresolved same-day MST
            # ambiguity into an arbitrary version-key tie break here.
            raise LawHistoryError("LAW_HISTORY_REFRESH_SAME_DAY_AMBIGUITY")
        if previous is None or source.effective_date > previous.effective_date:
            selected[source.law_id] = source
    rows: list[dict[str, object]] = []
    for law_id in LAW_HISTORY_LAW_IDS:
        selected_source = selected.get(law_id)
        if selected_source is None:
            continue
        rows.append(
            {
                "law_id": selected_source.law_id,
                "mst": selected_source.effective_mst,
                "version_key": selected_source.version_key,
                "effective_date": selected_source.effective_date.isoformat(),
                "raw_sha256": selected_source.effective_sha256,
                "is_repealed": selected_source.is_repealed,
            }
        )
    return rows


def _refresh_record(
    *, from_date: date, to_date: date, sources: Sequence[LawHistorySource]
) -> dict[str, object]:
    source_rows = _source_receipt_rows(sources)
    as_of_rows = _as_of_source_rows(sources, to_date=to_date)
    source_sha256 = sha256_bytes(canonical_json_bytes(source_rows))
    identity = {
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "sources": source_rows,
        "as_of_sources": as_of_rows,
    }
    refresh_id = sha256_bytes(canonical_json_bytes(identity))
    return {
        "refresh_id": refresh_id,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "source_count": len(source_rows),
        "source_sha256": source_sha256,
        "sources": source_rows,
        "as_of_sources": as_of_rows,
    }


def discard_active_law_history_manifest(*, local_root_override: Path | None = None) -> bool:
    """Remove only the active marker after an explicit profile-store reset.

    Raw official source batches and compact local benchmark artifacts remain
    available for maintenance; this helper is deliberately not called by a
    build or refresh failure path.
    """

    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    with _law_history_mutation_lock(profile):
        path = _active_manifest_path(profile)
        if not path.exists() and not path.is_symlink():
            return False
        if path.is_symlink() or not path.is_file():
            raise LawHistoryError("LAW_HISTORY_MANIFEST_PATH_UNSAFE")
        if _pending_refresh_path(profile).exists() or _pending_refresh_path(profile).is_symlink():
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        try:
            path.unlink()
        except OSError as exc:
            raise LawHistoryError("LAW_HISTORY_MANIFEST_RESET_FAILED") from exc
        return True


def _write_active_manifest(
    profile: Path, manifest: Mapping[str, object], *, replace: bool = False
) -> str:
    path = _active_manifest_path(profile)
    payload = canonical_json_bytes(manifest)
    _atomic_bytes(path, payload, replace=replace)
    return sha256_bytes(payload)


def _write_active_manifest_if_absent(profile: Path, manifest: Mapping[str, object]) -> str:
    """Publish one fully verified baseline without replacing a concurrent marker.

    The normal build path is already serialized by the profile mutation lock.
    Recovery additionally needs an absent-only publication primitive: a stale
    or manually created marker must never be overwritten after its expensive
    source/store verification has finished.  A same-directory hard link gives
    the final target an atomic no-replace creation step; a crash before that
    step leaves only a disposable temporary file, while a crash after it
    leaves a complete marker for the explicit published-state recovery path.
    """

    path = _active_manifest_path(profile)
    if path.exists() or path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_MANIFEST_CONCURRENT_MODIFICATION")
    payload = canonical_json_bytes(manifest)
    descriptor: int | None = None
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".recover.tmp", dir=path.parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise LawHistoryError("LAW_HISTORY_MANIFEST_CONCURRENT_MODIFICATION") from exc
    except LawHistoryError:
        raise
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_OUTPUT_WRITE_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return sha256_bytes(payload)


def _replace_active_manifest_if_digest(
    *, profile: Path, expected_sha256: str, manifest: Mapping[str, object]
) -> str:
    """Compare-and-swap the active pointer while its writer lock is held."""

    _current, actual_sha256 = _load_active_manifest(profile)
    if actual_sha256 != expected_sha256:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_CONCURRENT_MODIFICATION")
    return _write_active_manifest(profile, manifest, replace=True)


def _replace_active_manifest_bytes_if_digest(
    *, profile: Path, expected_sha256: str, manifest: Mapping[str, object]
) -> str:
    """CAS-replace a marker that is intentionally unreadable by the normal validator.

    The caller must already hold the profile writer lock and have completed the
    constrained stale-receipt validation.  Reading raw bytes here avoids
    treating the one known-stale receipt as a normal active manifest while
    retaining the same concurrent-modification protection as ordinary marker
    mutations.
    """

    path = _active_manifest_path(profile)
    if not path.is_file() or path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_MANIFEST_CONCURRENT_MODIFICATION")
    try:
        actual_sha256 = sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_CONCURRENT_MODIFICATION") from exc
    if actual_sha256 != expected_sha256:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_CONCURRENT_MODIFICATION")
    return _write_active_manifest(profile, manifest, replace=True)


def bind_law_history_retrieval_strategy(
    *,
    strategy: Literal["rrf_lexical", "cross_encoder_rerank"],
    expected_manifest_sha256: str,
    benchmark_result_sha256: str,
    local_root_override: Path | None = None,
) -> str:
    """Explicitly bind one reviewed, deployable benchmark result to serving."""

    if strategy not in {"rrf_lexical", "cross_encoder_rerank"}:
        raise LawHistoryError("LAW_HISTORY_BENCHMARK_CANDIDATE_NOT_DEPLOYABLE")
    if (
        _SHA256.fullmatch(expected_manifest_sha256) is None
        or _SHA256.fullmatch(benchmark_result_sha256) is None
    ):
        raise LawHistoryError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    with _law_history_mutation_lock(profile):
        # A benchmark is meaningful only if it measured the exact PG↔Qdrant
        # projection that will serve after the strategy marker changes.  The
        # lightweight state validator deliberately checks only request-time
        # invariants; this maintenance mutation therefore requires the full
        # projection audit as an additional publish gate.
        verified = verify_law_history_store(local_root_override=local_root_override)
        verified_digest = verified.get("manifest_sha256")
        if not isinstance(verified_digest, str) or verified_digest != expected_manifest_sha256:
            raise LawHistoryError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
        manifest, active_digest = validate_law_history_state(
            local_root_override=local_root_override
        )
        if active_digest != expected_manifest_sha256:
            raise LawHistoryError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
        if manifest.get("temporal_store_projection_sha256") != verified.get(
            "temporal_store_projection_sha256"
        ):
            raise LawHistoryError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
        if manifest.get("source_article_projection_sha256") != verified.get(
            "source_article_projection_sha256"
        ):
            raise LawHistoryError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
        updated = dict(manifest)
        updated["retrieval_strategy"] = strategy
        updated["retrieval_strategy_benchmark_sha256"] = benchmark_result_sha256
        return _replace_active_manifest_if_digest(
            profile=profile,
            expected_sha256=expected_manifest_sha256,
            manifest=updated,
        )


def _manifest_from_sources(
    *,
    profile: Path,
    source_root: Path,
    baseline_sources: Sequence[LawHistorySource],
    models: Mapping[str, object],
    counts: Mapping[str, int],
    source_article_projection_sha256: str,
    temporal_store_projection_sha256: str,
    frozen_inputs: Mapping[str, object],
    coverage_through: date,
    refreshes: Sequence[Mapping[str, object]] = (),
    retrieval_strategy: Literal["rrf_lexical", "cross_encoder_rerank"] = "rrf_lexical",
) -> dict[str, object]:
    return {
        "local_state_version": LAW_HISTORY_LOCAL_STATE_VERSION,
        "profile": LAW_HISTORY_PROFILE,
        "coverage_from": LAW_HISTORY_BASELINE_AS_OF.isoformat(),
        "coverage_through": coverage_through.isoformat(),
        "law_ids": list(LAW_HISTORY_LAW_IDS),
        "source_assets": [
            source.local_state(profile=profile, source_root=source_root)
            for source in baseline_sources
        ],
        "model_ids/revisions": dict(models),
        "store_counts": dict(counts),
        "source_article_projection_sha256": source_article_projection_sha256,
        "temporal_store_projection_sha256": temporal_store_projection_sha256,
        "frozen_inputs": dict(frozen_inputs),
        "retrieval_strategy": retrieval_strategy,
        "retrieval_strategy_benchmark_sha256": None,
        "refreshes": [dict(value) for value in refreshes],
        "data_build_timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _validate_active_manifest_payload(
    value: object,
    *,
    rebind_frozen_inputs: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate the active marker shape and its append-only receipt chain.

    ``rebind_frozen_inputs`` is intentionally private and only used by the
    explicit fixed-input receipt repair.  It accepts a marker only when its
    sole frozen-input difference is the narrowly checked stale CCCT receipt.
    """

    manifest = value
    required = {
        "local_state_version",
        "profile",
        "coverage_from",
        "coverage_through",
        "law_ids",
        "source_assets",
        "model_ids/revisions",
        "store_counts",
        "source_article_projection_sha256",
        "temporal_store_projection_sha256",
        "frozen_inputs",
        "retrieval_strategy",
        "retrieval_strategy_benchmark_sha256",
        "refreshes",
        "data_build_timestamp",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if (
        manifest["local_state_version"] != LAW_HISTORY_LOCAL_STATE_VERSION
        or manifest["profile"] != LAW_HISTORY_PROFILE
        or manifest["coverage_from"] != LAW_HISTORY_BASELINE_AS_OF.isoformat()
        or manifest["law_ids"] != list(LAW_HISTORY_LAW_IDS)
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_MISMATCH")
    if manifest["retrieval_strategy"] not in {"rrf_lexical", "cross_encoder_rerank"}:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    benchmark_digest = manifest["retrieval_strategy_benchmark_sha256"]
    if benchmark_digest is not None and (
        not isinstance(benchmark_digest, str) or _SHA256.fullmatch(benchmark_digest) is None
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if (
        not isinstance(manifest["source_article_projection_sha256"], str)
        or _SHA256.fullmatch(cast(str, manifest["source_article_projection_sha256"])) is None
        or not isinstance(manifest["temporal_store_projection_sha256"], str)
        or _SHA256.fullmatch(cast(str, manifest["temporal_store_projection_sha256"])) is None
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if (
        manifest["retrieval_strategy"] == "cross_encoder_rerank"
        and manifest["retrieval_strategy_benchmark_sha256"] is None
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if rebind_frozen_inputs is None:
        _validate_frozen_inputs_manifest(manifest["frozen_inputs"])
    else:
        try:
            _validate_frozen_inputs_manifest(manifest["frozen_inputs"])
        except LawHistoryError as exc:
            if (
                str(exc) != "LAW_HISTORY_FROZEN_INPUT_MANIFEST_INVALID"
                or not _is_rebindable_stale_frozen_inputs(
                    stale=manifest["frozen_inputs"], current=rebind_frozen_inputs
                )
            ):
                raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_REBIND_NOT_PERMITTED") from exc
        else:
            raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_REBIND_NOT_REQUIRED")
    try:
        coverage_through = date.fromisoformat(cast(str, manifest["coverage_through"]))
    except (TypeError, ValueError) as exc:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
    if coverage_through < LAW_HISTORY_BASELINE_AS_OF:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if not isinstance(manifest["source_assets"], list) or not manifest["source_assets"]:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    _validated_refresh_records(
        refreshes=manifest["refreshes"],
        source_assets=manifest["source_assets"],
        coverage_through=coverage_through,
    )
    return manifest


def _load_active_manifest(profile: Path) -> tuple[dict[str, object], str]:
    path = _active_manifest_path(profile)
    if not path.is_file() or path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_MANIFEST_UNAVAILABLE")
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
    return _validate_active_manifest_payload(manifest), sha256_bytes(raw)


def _load_rebindable_frozen_input_manifest(
    *, profile: Path, current_frozen_inputs: Mapping[str, object]
) -> tuple[dict[str, object], str]:
    """Read only the narrowly stale marker accepted by the repair command."""

    path = _active_manifest_path(profile)
    if not path.is_file() or path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_MANIFEST_UNAVAILABLE")
    try:
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
    return (
        _validate_active_manifest_payload(manifest, rebind_frozen_inputs=current_frozen_inputs),
        sha256_bytes(raw),
    )


def _validated_refresh_records(
    *,
    refreshes: object,
    source_assets: object,
    coverage_through: date,
) -> tuple[dict[str, object], ...]:
    """Validate an append-only, gap-free refresh chain before it is trusted."""

    if not isinstance(refreshes, list) or not isinstance(source_assets, list):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    assets_by_key: dict[tuple[str, str], tuple[str, str, bool]] = {}
    for asset in source_assets:
        if not isinstance(asset, dict):
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        law_id = asset.get("law_id")
        mst = asset.get("effective_mst")
        version_key = asset.get("version_key")
        effective_date = asset.get("effective_date")
        raw_sha256 = asset.get("effective_sha256")
        is_repealed = asset.get("is_repealed")
        if (
            not isinstance(law_id, str)
            or not isinstance(mst, str)
            or not isinstance(version_key, str)
            or not isinstance(effective_date, str)
            or not isinstance(raw_sha256, str)
            or type(is_repealed) is not bool
        ):
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        try:
            parsed_effective_date = date.fromisoformat(effective_date)
        except ValueError as exc:
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
        if not _valid_law_history_version_key(
            version_key,
            mst=mst,
            effective_date=parsed_effective_date,
        ):
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        key = (law_id, version_key)
        if key in assets_by_key:
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        assets_by_key[key] = (effective_date, raw_sha256, is_repealed)

    expected_from = LAW_HISTORY_BASELINE_AS_OF + timedelta(days=1)
    parsed: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for raw in refreshes:
        required_keys = {
            "refresh_id",
            "from",
            "to",
            "source_count",
            "source_sha256",
            "sources",
            "as_of_sources",
        }
        allowed_keys = {frozenset(required_keys), frozenset({*required_keys, "receipt_assets"})}
        if not isinstance(raw, dict) or frozenset(raw) not in allowed_keys:
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        refresh_id = raw["refresh_id"]
        source_sha256 = raw["source_sha256"]
        source_rows = raw["sources"]
        as_of_rows = raw["as_of_sources"]
        if (
            not isinstance(refresh_id, str)
            or _SHA256.fullmatch(refresh_id) is None
            or refresh_id in seen_ids
            or not isinstance(source_sha256, str)
            or _SHA256.fullmatch(source_sha256) is None
            or not isinstance(source_rows, list)
            or not isinstance(as_of_rows, list)
            or type(raw["source_count"]) is not int
            or raw["source_count"] != len(source_rows)
            or not source_rows
        ):
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        try:
            from_date = date.fromisoformat(cast(str, raw["from"]))
            to_date = date.fromisoformat(cast(str, raw["to"]))
        except (TypeError, ValueError) as exc:
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID") from exc
        if from_date != expected_from or to_date < from_date:
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        receipt_by_key: dict[tuple[str, str], tuple[str, str, str, bool]] = {}
        raw_receipt_assets = raw.get("receipt_assets")
        if raw_receipt_assets is not None:
            if not isinstance(raw_receipt_assets, list) or len(raw_receipt_assets) != len(
                source_rows
            ):
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            for receipt_asset in raw_receipt_assets:
                if not isinstance(receipt_asset, dict) or set(receipt_asset) != {
                    "law_id",
                    "current_mst",
                    "current_file",
                    "current_sha256",
                    "effective_mst",
                    "version_key",
                    "effective_file",
                    "effective_sha256",
                    "effective_date",
                    "article_count",
                    "is_repealed",
                    "law_name",
                    "hierarchy",
                    "ministry",
                    "official_uri",
                    "collected_at",
                    "source_kind",
                }:
                    raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
                try:
                    receipt = _source_from_asset(receipt_asset)
                except LawHistoryError as exc:
                    raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID") from exc
                if receipt.source_kind != "refresh":
                    raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
                receipt_key = (receipt.law_id, receipt.version_key)
                if receipt_key in receipt_by_key:
                    raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
                receipt_by_key[receipt_key] = (
                    receipt.effective_mst,
                    receipt.effective_date.isoformat(),
                    receipt.effective_sha256,
                    receipt.is_repealed,
                )
        normalized_rows: list[dict[str, object]] = []
        for row in source_rows:
            if not isinstance(row, dict) or set(row) != {
                "law_id",
                "mst",
                "version_key",
                "effective_date",
                "raw_sha256",
                "is_repealed",
            }:
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            law_id = row["law_id"]
            mst = row["mst"]
            version_key = row["version_key"]
            effective_date = row["effective_date"]
            raw_sha = row["raw_sha256"]
            is_repealed = row["is_repealed"]
            if (
                not isinstance(law_id, str)
                or law_id not in LAW_HISTORY_LAW_IDS
                or not isinstance(mst, str)
                or _MST.fullmatch(mst) is None
                or not isinstance(version_key, str)
                or not isinstance(effective_date, str)
                or not isinstance(raw_sha, str)
                or _SHA256.fullmatch(raw_sha) is None
                or type(is_repealed) is not bool
            ):
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            try:
                parsed_effective_date = date.fromisoformat(effective_date)
            except ValueError as exc:
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID") from exc
            if not _valid_law_history_version_key(
                version_key,
                mst=mst,
                effective_date=parsed_effective_date,
            ):
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            canonical = assets_by_key.get((law_id, version_key))
            if canonical is None or canonical[0] != effective_date or canonical[2] != is_repealed:
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            if receipt_by_key:
                if receipt_by_key.get((law_id, version_key)) != (
                    mst,
                    effective_date,
                    raw_sha,
                    is_repealed,
                ):
                    raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            elif canonical[1] != raw_sha:
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            normalized_rows.append(
                {
                    "law_id": law_id,
                    "mst": mst,
                    "version_key": version_key,
                    "effective_date": effective_date,
                    "raw_sha256": raw_sha,
                    "is_repealed": is_repealed,
                }
            )
        sorted_rows = sorted(
            normalized_rows,
            key=lambda value: (value["effective_date"], value["law_id"], value["version_key"]),
        )
        if normalized_rows != sorted_rows or len(
            {(row["law_id"], row["version_key"]) for row in sorted_rows}
        ) != len(sorted_rows):
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        if receipt_by_key and set(receipt_by_key) != {
            (cast(str, row["law_id"]), cast(str, row["version_key"])) for row in sorted_rows
        }:
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        if sha256_bytes(canonical_json_bytes(sorted_rows)) != source_sha256:
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        selected: dict[str, dict[str, object]] = {}
        for row in sorted_rows:
            try:
                effective_date = date.fromisoformat(cast(str, row["effective_date"]))
            except (TypeError, ValueError) as exc:
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID") from exc
            if effective_date > to_date:
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            law_id = cast(str, row["law_id"])
            previous = selected.get(law_id)
            if previous is not None:
                previous_date = date.fromisoformat(cast(str, previous["effective_date"]))
                if effective_date == previous_date:
                    raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            if previous is None or effective_date > date.fromisoformat(
                cast(str, previous["effective_date"])
            ):
                selected[law_id] = row
        expected_as_of_rows = [
            selected[law_id] for law_id in LAW_HISTORY_LAW_IDS if law_id in selected
        ]
        if (
            len(expected_as_of_rows) != len(LAW_HISTORY_LAW_IDS)
            or as_of_rows != expected_as_of_rows
        ):
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        identity = {
            "from": from_date.isoformat(),
            "to": to_date.isoformat(),
            "sources": sorted_rows,
            "as_of_sources": expected_as_of_rows,
        }
        if sha256_bytes(canonical_json_bytes(identity)) != refresh_id:
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        seen_ids.add(refresh_id)
        parsed.append(raw)
        expected_from = to_date + timedelta(days=1)
    if coverage_through != expected_from - timedelta(days=1):
        raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
    return tuple(parsed)


def _validate_refresh_receipt_assets(
    *,
    profile: Path,
    refreshes: Sequence[Mapping[str, object]],
    source_assets: Sequence[Mapping[str, object]],
) -> None:
    """Verify retained refresh receipts when one legal version has raw variants.

    A canonical source asset owns the indexed legal version and its first
    immutable raw receipt. A refresh can retain another official response for
    that exact version only when its contact-redacted, hash-verified body is
    equal; a raw-body difference alone must never hide an article or legal-
    metadata change.
    """

    canonical_by_key: dict[tuple[str, str], Mapping[str, object]] = {}
    for asset in source_assets:
        law_id = asset.get("law_id")
        version_key = asset.get("version_key")
        if not isinstance(law_id, str) or not isinstance(version_key, str):
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        canonical_by_key[(law_id, version_key)] = asset
    receipt_content_cache: dict[tuple[str, str, str], str] = {}

    def receipt_content(asset: Mapping[str, object]) -> str:
        law_id = cast(str, asset["law_id"])
        version_key = cast(str, asset["version_key"])
        raw_sha256 = cast(str, asset["effective_sha256"])
        key = (law_id, version_key, raw_sha256)
        cached = receipt_content_cache.get(key)
        if cached is None:
            cached = _source_receipt_content_sha256(profile, asset)
            receipt_content_cache[key] = cached
        return cached

    for refresh in refreshes:
        raw_receipt_assets = refresh.get("receipt_assets")
        if raw_receipt_assets is None:
            continue
        if not isinstance(raw_receipt_assets, list):
            raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
        for raw_receipt in raw_receipt_assets:
            if not isinstance(raw_receipt, dict):
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            _validate_source_asset(profile, raw_receipt)
            receipt = _source_from_asset(raw_receipt)
            if receipt.source_kind != "refresh":
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            canonical = canonical_by_key.get((receipt.law_id, receipt.version_key))
            if canonical is None:
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            _validate_source_asset(profile, canonical)
            if (
                canonical.get("effective_mst") != receipt.effective_mst
                or canonical.get("effective_date") != receipt.effective_date.isoformat()
                or canonical.get("is_repealed") != receipt.is_repealed
            ):
                raise LawHistoryError("LAW_HISTORY_REFRESH_SOURCE_CONFLICT")
            if canonical.get("effective_sha256") != receipt.effective_sha256 and receipt_content(
                canonical
            ) != receipt_content(raw_receipt):
                raise LawHistoryError("LAW_HISTORY_REFRESH_SOURCE_CONFLICT")


def _validate_refresh_ledger(
    refreshes: Sequence[Mapping[str, object]], *, allow_unrecorded_final: bool = False
) -> None:
    """Require the durable ledger to match an active or pre-publish chain.

    A ready refresh journal is fully verified before its ledger row is written.
    During that short transaction boundary, the ledger may validly contain the
    immediately preceding chain, but it may never contain a partial or extra
    record.  Once the row is recorded, callers use the default exact mode.
    """

    from src.db.postgres import connect

    expected = {
        (
            cast(str, record["refresh_id"]),
            date.fromisoformat(cast(str, record["from"])),
            date.fromisoformat(cast(str, record["to"])),
            cast(str, record["source_sha256"]),
        )
        for record in refreshes
    }
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT refresh_id, from_date, to_date, refresh_sha256
                FROM law_history_refreshes
                WHERE profile=%s
                """,
                (LAW_HISTORY_PROFILE,),
            )
            rows = cur.fetchall()
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_REFRESH_LEDGER_UNAVAILABLE") from exc
    observed = {
        (str(row[0]), row[1], row[2], str(row[3]))
        for row in rows
        if isinstance(row, tuple) and len(row) == 4
    }
    allowed = {frozenset(expected)}
    if allow_unrecorded_final and refreshes:
        preceding = {
            (
                cast(str, record["refresh_id"]),
                date.fromisoformat(cast(str, record["from"])),
                date.fromisoformat(cast(str, record["to"])),
                cast(str, record["source_sha256"]),
            )
            for record in refreshes[:-1]
        }
        allowed.add(frozenset(preceding))
    if len(observed) != len(rows) or frozenset(observed) not in allowed:
        raise LawHistoryError("LAW_HISTORY_REFRESH_LEDGER_MISMATCH")


def _validate_source_asset(profile: Path, asset: Mapping[str, object]) -> None:
    expected_keys = {
        "law_id",
        "current_mst",
        "current_file",
        "current_sha256",
        "effective_mst",
        "version_key",
        "effective_file",
        "effective_sha256",
        "effective_date",
        "article_count",
        "is_repealed",
        "law_name",
        "hierarchy",
        "ministry",
        "official_uri",
        "collected_at",
        "source_kind",
    }
    if set(asset) != expected_keys:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    law_id = asset["law_id"]
    if not isinstance(law_id, str) or law_id not in LAW_HISTORY_LAW_IDS:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if asset["source_kind"] not in {"baseline", "refresh"}:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    current_mst = asset["current_mst"]
    effective_mst = asset["effective_mst"]
    version_key = asset["version_key"]
    if (
        not isinstance(current_mst, str)
        or _MST.fullmatch(current_mst) is None
        or not isinstance(effective_mst, str)
        or _MST.fullmatch(effective_mst) is None
        or asset["official_uri"] != _official_uri(effective_mst)
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    is_repealed = asset["is_repealed"]
    if type(is_repealed) is not bool:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if (
        type(asset["article_count"]) is not int
        or cast(int, asset["article_count"]) < 0
        or (cast(int, asset["article_count"]) == 0) != is_repealed
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    law_name = asset["law_name"]
    hierarchy = asset["hierarchy"]
    ministry = asset["ministry"]
    if (
        not isinstance(law_name, str)
        or not law_name
        or hierarchy != _law_hierarchy(law_name)
        or not isinstance(ministry, str)
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    try:
        asset_effective_date = date.fromisoformat(cast(str, asset["effective_date"]))
        collected_at = datetime.fromisoformat(cast(str, asset["collected_at"]))
    except (TypeError, ValueError) as exc:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if not _valid_law_history_version_key(
        version_key,
        mst=cast(str, effective_mst),
        effective_date=asset_effective_date,
    ):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    from src.ingest.laws import build_meta, parse_articles

    for kind in ("current", "effective"):
        relative = asset[f"{kind}_file"]
        expected = asset[f"{kind}_sha256"]
        if (
            not isinstance(relative, str)
            or not isinstance(expected, str)
            or _SHA256.fullmatch(expected) is None
        ):
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        candidate = _profile_source_file(profile, relative)
        if not candidate.is_file() or candidate.is_symlink():
            raise LawHistoryError("LAW_HISTORY_SOURCE_MISSING")
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            raise LawHistoryError("LAW_HISTORY_SOURCE_MISSING") from exc
        if sha256_bytes(raw) != expected:
            raise LawHistoryError("LAW_HISTORY_SOURCE_HASH_MISMATCH")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LawHistoryError("LAW_HISTORY_SOURCE_INVALID") from exc
        mst = cast(str, asset[f"{kind}_mst"])
        meta = build_meta(body, mst)
        if meta.get("law_id") != law_id:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ID_MISMATCH")
        if (
            meta.get("law_name") != law_name
            or meta.get("ministry") != ministry
            or _is_repeal_revision(meta.get("revision_type")) != is_repealed
        ):
            raise LawHistoryError("LAW_HISTORY_SOURCE_METADATA_INVALID")
        if kind == "effective":
            try:
                effective_date = datetime.strptime(str(meta["enforcement_date"]), "%Y%m%d").date()
            except (KeyError, TypeError, ValueError) as exc:
                raise LawHistoryError("LAW_HISTORY_EFFECTIVE_DATE_INVALID") from exc
            if effective_date.isoformat() != asset["effective_date"]:
                raise LawHistoryError("LAW_HISTORY_EFFECTIVE_DATE_INVALID")
            rows = parse_articles(body, meta)
            article_count = sum(1 + len(row.get("children", [])) for row in rows)
            if article_count != asset["article_count"]:
                raise LawHistoryError("LAW_HISTORY_ARTICLE_COUNT_MISMATCH")


def _validate_store_source_provenance(source_assets: Sequence[Mapping[str, object]]) -> None:
    """Cross-check every manifest receipt against the isolated temporal DB.

    Raw files remain profile-local.  PostgreSQL stores only the receipt fields
    necessary to prove which immutable law version generated each temporal
    row.  The profile is isolated, so an exact set comparison catches both a
    missing source and an unmanifested extra version after a failed refresh.
    """

    from src.db.postgres import connect

    expected: dict[tuple[str, str], tuple[object, ...]] = {}
    for asset in source_assets:
        key = (cast(str, asset["law_id"]), cast(str, asset["version_key"]))
        try:
            collected_at = datetime.fromisoformat(cast(str, asset["collected_at"]))
            effective_date = date.fromisoformat(cast(str, asset["effective_date"]))
        except (TypeError, ValueError) as exc:
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
        expected[key] = (
            cast(str, asset["official_uri"]),
            cast(str, asset["effective_sha256"]),
            collected_at,
            effective_date,
            cast(int, asset["article_count"]),
            cast(bool, asset["is_repealed"]),
            cast(str, asset["law_name"]),
            cast(str, asset["hierarchy"]),
            cast(str, asset["ministry"]),
            cast(str, asset["source_kind"]),
        )
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT src.law_id, src.mst, src.version_key, src.official_uri, src.raw_sha256,
                       src.collected_at, src.effective_date, src.article_count,
                       src.is_repealed, version.law_name, version.hierarchy,
                       version.ministry, src.source_kind, version.enforcement_date
                FROM law_version_sources src
                JOIN law_versions version
                  ON version.law_id=src.law_id AND version.version_key=src.version_key
                """
            )
            rows = cur.fetchall()
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_SOURCE_PROVENANCE_UNAVAILABLE") from exc
    observed: dict[tuple[str, str], tuple[object, ...]] = {}
    for row in rows:
        if not isinstance(row, tuple) or len(row) != 14:
            raise LawHistoryError("LAW_HISTORY_SOURCE_PROVENANCE_MISMATCH")
        (
            law_id,
            mst,
            version_key,
            official_uri,
            raw_sha256,
            collected_at,
            effective_date,
            article_count,
            is_repealed,
            law_name,
            hierarchy,
            ministry,
            source_kind,
            version_date,
        ) = row
        if (
            not isinstance(version_key, str)
            or not isinstance(mst, str)
            or not isinstance(effective_date, date)
            or not _valid_law_history_version_key(
                version_key,
                mst=mst,
                effective_date=effective_date,
            )
        ):
            raise LawHistoryError("LAW_HISTORY_SOURCE_PROVENANCE_MISMATCH")
        key = (str(law_id), version_key)
        if key in observed or effective_date != version_date:
            raise LawHistoryError("LAW_HISTORY_SOURCE_PROVENANCE_MISMATCH")
        observed[key] = (
            str(official_uri),
            str(raw_sha256),
            collected_at,
            effective_date,
            int(article_count),
            bool(is_repealed),
            str(law_name),
            str(hierarchy),
            str(ministry),
            str(source_kind),
        )
    if set(observed) != set(expected):
        raise LawHistoryError("LAW_HISTORY_SOURCE_PROVENANCE_MISMATCH")
    for key, expected_value in expected.items():
        observed_value = observed[key]
        if observed_value != expected_value:
            raise LawHistoryError("LAW_HISTORY_SOURCE_PROVENANCE_MISMATCH")


def _recovery_source_batch_directory(profile: Path, source_batch: str) -> Path:
    """Resolve one direct, ignored baseline receipt directory without escapes."""

    if not isinstance(source_batch, str) or not source_batch:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
    value = Path(source_batch)
    if (
        value.is_absolute()
        or value.drive
        or value.root
        or len(value.parts) != 1
        or value.name != source_batch
        or not value.name.startswith("baseline-")
    ):
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
    parent = _safe_directory(profile / "data" / "law-history")
    candidate = parent / value.name
    try:
        candidate.resolve(strict=True).relative_to(parent.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID") from exc
    if not candidate.is_dir() or candidate.is_symlink():
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
    return candidate


def _recovery_baseline_entries(
    *, profile: Path, source_batch: str
) -> tuple[Path, dict[tuple[str, str], dict[str, object]]]:
    """Load a complete local baseline receipt manifest, never an arbitrary file set."""

    root = _recovery_source_batch_directory(profile, source_batch)
    manifest_path = root / _SOURCE_BATCH_MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
    try:
        raw = manifest_path.read_bytes()
        if len(raw) > 256 * 1024:
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
        manifest = json.loads(raw.decode("utf-8"))
    except LawHistoryError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"local_state_version", "profile", "as_of", "sources"}
        or manifest.get("local_state_version") != LAW_HISTORY_LOCAL_STATE_VERSION
        or manifest.get("profile") != LAW_HISTORY_PROFILE
        or manifest.get("as_of") != LAW_HISTORY_BASELINE_AS_OF.isoformat()
        or not isinstance(manifest.get("sources"), list)
    ):
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")

    entries: dict[tuple[str, str], dict[str, object]] = {}
    expected_files = {_SOURCE_BATCH_MANIFEST_NAME}
    for raw_entry in cast(list[object], manifest["sources"]):
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "law_id",
            "effective_mst",
            "version_key",
            "effective_sha256",
            "effective_date",
            "article_count",
            "is_repealed",
        }:
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
        law_id = raw_entry["law_id"]
        mst = raw_entry["effective_mst"]
        version_key = raw_entry["version_key"]
        digest = raw_entry["effective_sha256"]
        article_count = raw_entry["article_count"]
        is_repealed = raw_entry["is_repealed"]
        if (
            not isinstance(law_id, str)
            or law_id not in LAW_HISTORY_LAW_IDS
            or not isinstance(mst, str)
            or _MST.fullmatch(mst) is None
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or type(article_count) is not int
            or article_count < 1
            or is_repealed is not False
        ):
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
        try:
            effective_date = date.fromisoformat(cast(str, raw_entry["effective_date"]))
        except (TypeError, ValueError) as exc:
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID") from exc
        if effective_date > LAW_HISTORY_BASELINE_AS_OF or not _valid_law_history_version_key(
            version_key, mst=mst, effective_date=effective_date
        ):
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
        key = (law_id, cast(str, version_key))
        filename = f"law_version_{law_id}_{mst}_{effective_date:%Y%m%d}.json"
        if key in entries or filename in expected_files:
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
        entries[key] = dict(raw_entry)
        expected_files.add(filename)
    if len(entries) != len(LAW_HISTORY_LAW_IDS) or {key[0] for key in entries} != set(
        LAW_HISTORY_LAW_IDS
    ):
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
    try:
        files = tuple(root.iterdir())
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID") from exc
    if {item.name for item in files} != expected_files or any(
        item.is_symlink() or not item.is_file() for item in files
    ):
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_BATCH_INVALID")
    return root, entries


def _recoverable_baseline_sources(
    *, profile: Path, source_batch: str
) -> tuple[Path, tuple[LawHistorySource, ...]]:
    """Reconstruct an unpublished baseline only when local receipts equal PG provenance."""

    root, entries = _recovery_baseline_entries(profile=profile, source_batch=source_batch)
    from src.db.postgres import connect

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT src.law_id, src.mst, src.version_key, src.official_uri, src.raw_sha256,
                       src.collected_at, src.effective_date, src.article_count,
                       src.is_repealed, version.law_name, version.hierarchy,
                       version.ministry, src.source_kind, version.enforcement_date
                FROM law_version_sources src
                JOIN law_versions version
                  ON version.law_id=src.law_id AND version.version_key=src.version_key
                """
            )
            rows = cur.fetchall()
            cur.execute(
                """
                SELECT (SELECT count(*) FROM laws),
                       (SELECT count(*) FROM law_versions),
                       (SELECT count(*) FROM law_version_sources),
                       (SELECT count(*) FROM law_history_refreshes)
                """
            )
            counts = cur.fetchone()
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_SOURCE_PROVENANCE_UNAVAILABLE") from exc
    if (
        not isinstance(counts, tuple)
        or len(counts) != 4
        or any(type(value) is not int for value in counts)
    ):
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_PROVENANCE_MISMATCH")
    if tuple(counts) != (65, 65, 65, 0) or len(rows) != len(LAW_HISTORY_LAW_IDS):
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_PROVENANCE_MISMATCH")

    sources: list[LawHistorySource] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, tuple) or len(row) != 14:
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_PROVENANCE_MISMATCH")
        (
            law_id,
            mst,
            version_key,
            official_uri,
            raw_sha256,
            collected_at,
            effective_date,
            article_count,
            is_repealed,
            law_name,
            hierarchy,
            ministry,
            source_kind,
            version_date,
        ) = row
        if (
            not isinstance(law_id, str)
            or not isinstance(mst, str)
            or not isinstance(version_key, str)
            or not isinstance(official_uri, str)
            or not isinstance(raw_sha256, str)
            or not isinstance(collected_at, datetime)
            or collected_at.tzinfo is None
            or collected_at.utcoffset() is None
            or not isinstance(effective_date, date)
            or type(article_count) is not int
            or type(is_repealed) is not bool
            or not isinstance(law_name, str)
            or not isinstance(hierarchy, str)
            or not isinstance(ministry, str)
            or source_kind != "baseline"
            or is_repealed
            or version_date != effective_date
            or official_uri != _official_uri(mst)
            or not _valid_law_history_version_key(
                version_key, mst=mst, effective_date=effective_date
            )
        ):
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_PROVENANCE_MISMATCH")
        key = (law_id, version_key)
        entry = entries.get(key)
        if (
            key in seen
            or entry is None
            or (
                entry["effective_mst"] != mst
                or entry["effective_sha256"] != raw_sha256
                or entry["effective_date"] != effective_date.isoformat()
                or entry["article_count"] != article_count
                or entry["is_repealed"] is not False
            )
        ):
            raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_PROVENANCE_MISMATCH")
        filename = f"law_version_{law_id}_{mst}_{effective_date:%Y%m%d}.json"
        source = LawHistorySource(
            law_id=law_id,
            current_mst=mst,
            current_file=filename,
            current_sha256=raw_sha256,
            effective_mst=mst,
            effective_file=filename,
            effective_sha256=raw_sha256,
            effective_date=effective_date,
            article_count=article_count,
            is_repealed=False,
            law_name=law_name,
            hierarchy=hierarchy,
            ministry=ministry,
            official_uri=official_uri,
            collected_at=collected_at.isoformat(),
            source_kind="baseline",
        )
        _validate_source_asset(profile, source.local_state(profile=profile, source_root=root))
        seen.add(key)
        sources.append(source)
    if seen != set(entries):
        raise LawHistoryError("LAW_HISTORY_RECOVERY_SOURCE_PROVENANCE_MISMATCH")
    ordered = tuple(sorted(sources, key=lambda source: (source.law_id, source.version_key)))
    assets = [source.local_state(profile=profile, source_root=root) for source in ordered]
    _validate_store_source_provenance(assets)
    return root, ordered


def recover_law_history_baseline(
    *,
    source_batch: str,
    confirm_profile: str,
    local_root_override: Path | None = None,
) -> Path:
    """Publish an already-loaded baseline only after the same full build gates pass.

    This operator action is deliberately network-free: it is only for a build
    that completed isolated loading/indexing but crashed or was blocked before
    its active manifest could be written.  It never tolerates a partial store,
    a refresh, an existing marker, or a source receipt that does not exactly
    match PostgreSQL provenance.
    """

    if confirm_profile != LAW_HISTORY_PROFILE:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_CONFIRMATION_INVALID")
    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    with _law_history_mutation_lock(profile):
        try:
            apply_functional_uat_law_history_runtime_environment(root=local_root_override)
        except LocalProfileError as exc:
            raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
        active = _active_manifest_path(profile)
        if active.exists() or active.is_symlink():
            raise LawHistoryError("LAW_HISTORY_BUILD_RECOVERY_ACTIVE_MANIFEST")
        pending = _pending_refresh_path(profile)
        if pending.exists() or pending.is_symlink():
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        source_root, sources = _recoverable_baseline_sources(
            profile=profile, source_batch=source_batch
        )
        source_assets = [
            source.local_state(profile=profile, source_root=source_root) for source in sources
        ]
        attestation = build_frozen_input_attestation()
        _verify_frozen_input_projections_full(attestation)
        models = _law_history_model_inventory(
            profile=profile, local_root_override=local_root_override
        )
        counts = _history_store_counts()
        source_article_projection_sha256 = _verify_source_article_projection(
            profile=profile, source_assets=source_assets
        )
        temporal_store_projection_sha256 = _verify_temporal_store_projection()
        manifest = _manifest_from_sources(
            profile=profile,
            source_root=source_root,
            baseline_sources=sources,
            models=models,
            counts=counts,
            source_article_projection_sha256=source_article_projection_sha256,
            temporal_store_projection_sha256=temporal_store_projection_sha256,
            frozen_inputs=attestation.manifest,
            coverage_through=LAW_HISTORY_BASELINE_AS_OF,
        )
        published_digest = _write_active_manifest_if_absent(profile, manifest)
        _published, active_digest = validate_law_history_state(
            local_root_override=local_root_override
        )
        verified = verify_law_history_store(local_root_override=local_root_override)
        if active_digest != published_digest or verified.get("manifest_sha256") != published_digest:
            raise LawHistoryError("LAW_HISTORY_RECOVERY_POSTVERIFY_MISMATCH")
        return _new_public_artifact(as_of=LAW_HISTORY_BASELINE_AS_OF, counts=counts, models=models)


def rebind_law_history_frozen_inputs(
    *, confirm_profile: str, local_root_override: Path | None = None
) -> str:
    """Replace only one stale fixed-input receipt after full local verification.

    This is a network-free, explicit maintenance operation for a published
    marker whose sole invalid condition is the bounded CCCT tracked-byte
    receipt accepted by :func:`_is_rebindable_stale_frozen_inputs`.  It never
    collects a law, downloads a model, discards refresh history, or repairs an
    arbitrary manifest.  Every source, fixed-input, store, model, and temporal
    projection gate must pass before the marker is atomically rebound.
    """

    if confirm_profile != LAW_HISTORY_PROFILE:
        raise LawHistoryError("LAW_HISTORY_RECOVERY_CONFIRMATION_INVALID")
    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    with _law_history_mutation_lock(profile):
        try:
            apply_functional_uat_law_history_runtime_environment(root=local_root_override)
        except LocalProfileError as exc:
            raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
        pending = _pending_refresh_path(profile)
        if pending.exists() or pending.is_symlink():
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")

        # Freshly attest the exact current fixed inputs before accepting even
        # the constrained previous receipt layout.
        attestation = build_frozen_input_attestation()
        manifest, previous_digest = _load_rebindable_frozen_input_manifest(
            profile=profile,
            current_frozen_inputs=attestation.manifest,
        )
        _validate_loaded_law_history_state(
            profile=profile,
            manifest=manifest,
            frozen_inputs=attestation.manifest,
            local_root_override=local_root_override,
        )
        _verify_frozen_input_projections_full(attestation)

        source_assets = manifest["source_assets"]
        assert isinstance(source_assets, list)
        source_projection_sha256 = _verify_source_article_projection(
            profile=profile,
            source_assets=cast(Sequence[Mapping[str, object]], source_assets),
        )
        if source_projection_sha256 != manifest["source_article_projection_sha256"]:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        temporal_projection_sha256 = _verify_temporal_store_projection()
        if temporal_projection_sha256 != manifest["temporal_store_projection_sha256"]:
            raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")

        updated = dict(manifest)
        updated["frozen_inputs"] = dict(attestation.manifest)
        published_digest = _replace_active_manifest_bytes_if_digest(
            profile=profile,
            expected_sha256=previous_digest,
            manifest=updated,
        )
        _published, active_digest = validate_law_history_state(
            local_root_override=local_root_override
        )
        verified = verify_law_history_store(local_root_override=local_root_override)
        if active_digest != published_digest or verified.get("manifest_sha256") != published_digest:
            raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_REBIND_POSTVERIFY_MISMATCH")
        return published_digest


def _validate_loaded_law_history_state(
    *,
    profile: Path,
    manifest: Mapping[str, object],
    frozen_inputs: Mapping[str, object],
    local_root_override: Path | None,
) -> None:
    """Run every active-state gate after the marker shape was validated."""

    source_assets = manifest["source_assets"]
    assert isinstance(source_assets, list)
    refreshes = _validated_refresh_records(
        refreshes=manifest["refreshes"],
        source_assets=source_assets,
        coverage_through=date.fromisoformat(cast(str, manifest["coverage_through"])),
    )
    seen: set[tuple[str, str]] = set()
    baseline_laws: set[str] = set()
    baseline_count = 0
    for asset in source_assets:
        if not isinstance(asset, dict):
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        _validate_source_asset(profile, asset)
        key = (cast(str, asset["law_id"]), cast(str, asset["version_key"]))
        if key in seen:
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        seen.add(key)
        if asset["source_kind"] == "baseline":
            baseline_count += 1
            baseline_laws.add(key[0])
            if not _is_valid_baseline_asset(asset):
                raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    if baseline_laws != set(LAW_HISTORY_LAW_IDS) or baseline_count != len(LAW_HISTORY_LAW_IDS):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    _validate_refresh_receipt_assets(
        profile=profile,
        refreshes=refreshes,
        source_assets=cast(Sequence[Mapping[str, object]], source_assets),
    )
    _validate_store_source_provenance(source_assets)
    _validate_refresh_ledger(refreshes)
    _validate_frozen_input_projections(frozen_inputs)
    models = _law_history_model_inventory(profile=profile, local_root_override=local_root_override)
    if manifest["model_ids/revisions"] != models:
        raise LawHistoryError("LAW_HISTORY_MODEL_CACHE_MISMATCH")
    if manifest["store_counts"] != _history_store_counts():
        raise LawHistoryError("LAW_HISTORY_STORE_COUNT_MISMATCH")


def validate_law_history_state(
    *, local_root_override: Path | None = None
) -> tuple[dict[str, object], str]:
    """Verify active local source bytes, cached model revisions, and counts."""

    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
        apply_functional_uat_law_history_runtime_environment(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    pending = _pending_refresh_path(profile)
    if pending.exists() or pending.is_symlink():
        # A journal means the stores can contain staged but not yet manifest-
        # bound versions.  Serving through that state would hide a partial
        # refresh, so only the explicit refresh resume path may proceed.
        raise LawHistoryError("LAW_HISTORY_REFRESH_PENDING")
    manifest, digest = _load_active_manifest(profile)
    frozen_inputs = _validate_frozen_inputs_manifest(manifest["frozen_inputs"])
    _validate_loaded_law_history_state(
        profile=profile,
        manifest=manifest,
        frozen_inputs=frozen_inputs,
        local_root_override=local_root_override,
    )
    return manifest, digest


def verify_law_history_store(*, local_root_override: Path | None = None) -> dict[str, object]:
    """Perform the explicit official-source → PG → Qdrant projection audit."""

    manifest, manifest_sha256 = validate_law_history_state(local_root_override=local_root_override)
    attestation = build_frozen_input_attestation()
    if attestation.manifest != manifest["frozen_inputs"]:
        raise LawHistoryError("LAW_HISTORY_FROZEN_INPUT_PROJECTION_MISMATCH")
    _verify_frozen_input_projections_full(attestation)
    source_projection_sha256 = _verify_source_article_projection(
        profile=ensure_functional_uat_law_history_profile(root=local_root_override),
        source_assets=cast(Sequence[Mapping[str, object]], manifest["source_assets"]),
    )
    if source_projection_sha256 != manifest["source_article_projection_sha256"]:
        raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
    projection_sha256 = _verify_temporal_store_projection()
    if projection_sha256 != manifest["temporal_store_projection_sha256"]:
        raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
    return {
        "profile": LAW_HISTORY_PROFILE,
        "manifest_sha256": manifest_sha256,
        "source_article_projection_sha256": source_projection_sha256,
        "temporal_store_projection_sha256": projection_sha256,
    }


def build_law_history(
    *,
    as_of: date,
    local_root_override: Path | None = None,
    api_get: LawApiGet | None = None,
) -> Path:
    """Build the isolated, all-65 baseline profile from fresh official data."""

    if as_of != LAW_HISTORY_BASELINE_AS_OF:
        raise LawHistoryError("LAW_HISTORY_BASELINE_AS_OF_MISMATCH")
    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    try:
        validate_functional_uat_law_history_api_credential(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_CREDENTIAL_UNAVAILABLE") from exc
    with _law_history_mutation_lock(profile):
        try:
            apply_functional_uat_law_history_runtime_environment(root=local_root_override)
        except LocalProfileError as exc:
            raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
        if _pending_refresh_path(profile).exists() or _pending_refresh_path(profile).is_symlink():
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        try:
            from scripts.migrate import main as migrate_main

            migrate_main()
        except Exception as exc:
            raise LawHistoryError("LAW_HISTORY_MIGRATION_FAILED") from exc
        _assert_empty_law_history_stores(profile)
        models = prefetch_models(
            profile_directory=profile,
            cache_directory=shared_model_cache_directory(root=local_root_override),
            include_reranker=False,
        )
        from src.embed.bm25 import ENCODER_VERSION

        models = {**models, "bm25": {"encoder_version": ENCODER_VERSION}}
        # Validate the fixed HSK/CCCT contract inputs before any official-law
        # collection.  A bad local input must not leave an expensive partial
        # law source batch behind, and the returned in-memory objects are the
        # ones subsequently projected into the isolated stores.
        attestation = build_frozen_input_attestation()
        resolved_api_get = api_get
        if resolved_api_get is None:
            from src import lawgo

            resolved_api_get = lawgo.get
        try:
            with functional_uat_law_history_collection_environment(root=local_root_override):
                source_root, sources = collect_law_history_baseline(
                    profile=profile, as_of=as_of, api_get=resolved_api_get
                )
        except LocalProfileError as exc:
            raise LawHistoryError("LAW_HISTORY_CREDENTIAL_UNAVAILABLE") from exc
        _load_history_sources(profile=profile, source_root=source_root, sources=sources)
        _load_hsk_requirements_and_temporal_index(attestation)
        counts = _history_store_counts()
        _verify_frozen_input_projections_full(attestation)
        source_assets = [
            source.local_state(profile=profile, source_root=source_root) for source in sources
        ]
        source_article_projection_sha256 = _verify_source_article_projection(
            profile=profile,
            source_assets=source_assets,
        )
        temporal_store_projection_sha256 = _verify_temporal_store_projection()
        manifest = _manifest_from_sources(
            profile=profile,
            source_root=source_root,
            baseline_sources=sources,
            models=models,
            counts=counts,
            source_article_projection_sha256=source_article_projection_sha256,
            temporal_store_projection_sha256=temporal_store_projection_sha256,
            frozen_inputs=attestation.manifest,
            coverage_through=as_of,
        )
        _write_active_manifest(profile, manifest)
        return _new_public_artifact(as_of=as_of, counts=counts, models=models)


def _load_history_sources(
    *,
    profile: Path,
    source_root: Path,
    sources: Sequence[LawHistorySource],
) -> None:
    """Load a just-collected source batch, checking each raw hash first."""

    _load_history_source_assets(
        profile=profile,
        source_assets=[
            source.local_state(profile=profile, source_root=source_root) for source in sources
        ],
    )


def _source_from_asset(asset: Mapping[str, object]) -> LawHistorySource:
    try:
        source = LawHistorySource(
            law_id=cast(str, asset["law_id"]),
            current_mst=cast(str, asset["current_mst"]),
            current_file=cast(str, asset["current_file"]),
            current_sha256=cast(str, asset["current_sha256"]),
            effective_mst=cast(str, asset["effective_mst"]),
            effective_file=cast(str, asset["effective_file"]),
            effective_sha256=cast(str, asset["effective_sha256"]),
            effective_date=date.fromisoformat(cast(str, asset["effective_date"])),
            article_count=cast(int, asset["article_count"]),
            is_repealed=cast(bool, asset["is_repealed"]),
            law_name=cast(str, asset["law_name"]),
            hierarchy=cast(str, asset["hierarchy"]),
            ministry=cast(str, asset["ministry"]),
            official_uri=cast(str, asset["official_uri"]),
            collected_at=cast(str, asset["collected_at"]),
            source_kind=cast(Literal["baseline", "refresh"], asset["source_kind"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
    if asset.get("version_key") != source.version_key:
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    return source


def _load_history_source_assets(
    *, profile: Path, source_assets: Sequence[Mapping[str, object]]
) -> None:
    """Idempotently load staged local receipts, including an interrupted refresh."""

    from src.db.postgres import connect
    from src.ingest.laws import load_law_history_version

    for asset in sorted(
        source_assets,
        key=lambda value: (
            cast(str, value.get("effective_date")),
            cast(str, value.get("law_id")),
            cast(str, value.get("version_key")),
        ),
    ):
        source, meta, rows = _read_history_source_asset(profile, asset)
        with connect() as conn, conn.cursor() as cur:
            load_law_history_version(cur, meta, rows)
            _record_source_provenance(cur, source)


def _read_history_source_asset(
    profile: Path, asset: Mapping[str, object]
) -> tuple[LawHistorySource, dict[str, object], list[dict[str, object]]]:
    """Read one hash-verified effective body in the exact loader form."""

    from src.ingest.laws import build_meta, parse_articles

    _validate_source_asset(profile, asset)
    source = _source_from_asset(asset)
    body, _path = _load_source_body(profile, asset)
    meta = build_meta(body, source.effective_mst)
    if meta.get("law_id") != source.law_id:
        raise LawHistoryError("LAW_HISTORY_SOURCE_ID_MISMATCH")
    if (
        meta.get("law_name") != source.law_name
        or meta.get("ministry") != source.ministry
        or _law_hierarchy(source.law_name) != source.hierarchy
        or _is_repeal_revision(meta.get("revision_type")) != source.is_repealed
    ):
        raise LawHistoryError("LAW_HISTORY_SOURCE_METADATA_INVALID")
    rows = parse_articles(body, meta)
    if sum(1 + len(row.get("children", [])) for row in rows) != source.article_count:
        raise LawHistoryError("LAW_HISTORY_ARTICLE_COUNT_MISMATCH")
    meta["is_repealed"] = source.is_repealed
    meta["version_key"] = source.version_key
    return (
        source,
        cast(dict[str, object], meta),
        cast(list[dict[str, object]], rows),
    )


def _source_receipt_content_sha256(profile: Path, asset: Mapping[str, object]) -> str:
    """Hash a receipt after removing only its non-legal contact-department field.

    The full official body remains the default identity.  The service is
    allowed to revise the direct ``법령.기본정보.연락부서`` subtree without a
    new MST or effective date, but no other metadata, article, or legal field
    is treated as interchangeable.  This deliberately does *not* reuse the
    narrower parsed-article projection: publication/revision metadata remains
    part of the immutable receipt boundary.
    """

    _validate_source_asset(profile, asset)
    body, _path = _load_source_body(profile, asset)
    normalized = deepcopy(body)
    law = normalized.get("법령")
    if not isinstance(law, dict):
        raise LawHistoryError("LAW_HISTORY_SOURCE_INVALID")
    basic_info = law.get("기본정보")
    if not isinstance(basic_info, dict):
        raise LawHistoryError("LAW_HISTORY_SOURCE_INVALID")
    basic_info.pop("연락부서", None)
    return sha256_bytes(canonical_json_bytes(normalized))


def _flatten_history_source_rows(
    rows: Sequence[Mapping[str, object]],
) -> tuple[
    tuple[
        tuple[int, int | None],
        Mapping[str, object],
        tuple[int, int | None] | None,
    ],
    ...,
]:
    """Flatten parser output while preserving the parent identity contract."""

    flattened: list[
        tuple[
            tuple[int, int | None],
            Mapping[str, object],
            tuple[int, int | None] | None,
        ]
    ] = []
    seen: set[tuple[int, int | None]] = set()

    def row_key(row: Mapping[str, object], *, parent: bool) -> tuple[int, int | None]:
        article_no = row.get("article_no")
        paragraph_no = row.get("paragraph_no")
        if (
            type(article_no) is not int
            or (paragraph_no is not None and type(paragraph_no) is not int)
            or (parent and paragraph_no is not None)
            or (not parent and type(paragraph_no) is not int)
            or not isinstance(row.get("content"), str)
            or not isinstance(row.get("valid_from"), date)
            or (row.get("title") is not None and not isinstance(row.get("title"), str))
        ):
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        return article_no, paragraph_no

    for parent_row in rows:
        parent_key = row_key(parent_row, parent=True)
        if parent_key in seen:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        seen.add(parent_key)
        flattened.append((parent_key, parent_row, None))
        children = parent_row.get("children")
        if not isinstance(children, list):
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        for child in children:
            if not isinstance(child, Mapping):
                raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
            child_key = row_key(child, parent=False)
            if child_key in seen:
                raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
            seen.add(child_key)
            flattened.append((child_key, child, parent_key))
    return tuple(flattened)


def _source_article_projection_records(
    *, profile: Path, source_assets: Sequence[Mapping[str, object]]
) -> tuple[dict[str, object], ...]:
    """Reconstruct the complete temporal article ledger from official bodies.

    ``load_law_history_version`` deliberately keeps an unchanged article's
    original MST.  Reconstructing the same append-only state from every
    verified local body lets the full verifier prove raw body → PostgreSQL
    identity, rather than only trusting a stored source hash or row count.
    """

    all_rows: list[_ProjectedSourceArticle] = []
    open_rows: dict[str, dict[tuple[int, int | None], _ProjectedSourceArticle]] = {}
    previous_effective: dict[str, date] = {}
    seen_sources: set[tuple[str, str]] = set()
    ordered_assets = sorted(
        source_assets,
        key=lambda value: (
            cast(str, value.get("effective_date")),
            cast(str, value.get("law_id")),
            cast(str, value.get("version_key")),
        ),
    )
    for asset in ordered_assets:
        source, meta, rows = _read_history_source_asset(profile, asset)
        source_key = (source.law_id, source.version_key)
        if source_key in seen_sources:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        seen_sources.add(source_key)
        try:
            enforcement = datetime.strptime(cast(str, meta["enforcement_date"]), "%Y%m%d").date()
        except (KeyError, TypeError, ValueError) as exc:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH") from exc
        if enforcement != source.effective_date:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        previous = previous_effective.get(source.law_id)
        if previous is not None and enforcement <= previous:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        current_open = open_rows.setdefault(source.law_id, {})
        if source.is_repealed:
            if previous is None:
                raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
            for projected in current_open.values():
                if enforcement <= projected.valid_from:
                    raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
                projected.valid_to = enforcement
            current_open.clear()
            previous_effective[source.law_id] = enforcement
            continue

        selected: dict[tuple[int, int | None], _ProjectedSourceArticle] = {}
        retained_or_replaced: set[tuple[int, int | None]] = set()
        for key, row, parent_key in _flatten_history_source_rows(rows):
            parent = selected.get(parent_key) if parent_key is not None else None
            if parent_key is not None and parent is None:
                raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
            content = cast(str, row["content"])
            valid_from = cast(date, row["valid_from"])
            existing = current_open.get(key)
            if (
                existing is not None
                and existing.content_hash == sha256_bytes(content.encode("utf-8"))
                and existing.parent is parent
            ):
                if existing.valid_from != valid_from:
                    raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
                selected[key] = existing
                retained_or_replaced.add(key)
                continue
            if existing is not None:
                if valid_from <= existing.valid_from:
                    raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
                existing.valid_to = valid_from
            elif previous is not None and valid_from < enforcement:
                raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
            projected = _ProjectedSourceArticle(
                law_id=source.law_id,
                article_no=key[0],
                paragraph_no=key[1],
                title=cast(str | None, row["title"]),
                content_hash=sha256_bytes(content.encode("utf-8")),
                version_mst=source.effective_mst,
                version_key=source.version_key,
                valid_from=valid_from,
                valid_to=None,
                parent=parent,
            )
            current_open[key] = projected
            selected[key] = projected
            retained_or_replaced.add(key)
            all_rows.append(projected)
        for key, projected in tuple(current_open.items()):
            if key in retained_or_replaced:
                continue
            if enforcement <= projected.valid_from:
                raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
            projected.valid_to = enforcement
            del current_open[key]
        previous_effective[source.law_id] = enforcement

    records: list[dict[str, object]] = []
    for projected in all_rows:
        parent = projected.parent
        records.append(
            {
                "law_id": projected.law_id,
                "article_no": projected.article_no,
                "paragraph_no": projected.paragraph_no,
                "version_mst": projected.version_mst,
                "version_key": projected.version_key,
                "title": projected.title,
                "content_hash": projected.content_hash,
                "valid_from": projected.valid_from.isoformat(),
                "valid_to": projected.valid_to.isoformat() if projected.valid_to else None,
                "parent_law_id": parent.law_id if parent else None,
                "parent_article_no": parent.article_no if parent else None,
                "parent_paragraph_no": parent.paragraph_no if parent else None,
                "parent_version_mst": parent.version_mst if parent else None,
                "parent_version_key": parent.version_key if parent else None,
            }
        )
    records.sort(
        key=lambda value: (
            cast(str, value["law_id"]),
            cast(int, value["article_no"]),
            -1 if value["paragraph_no"] is None else cast(int, value["paragraph_no"]),
            cast(str, value["version_key"]),
        )
    )
    return tuple(records)


def _verify_source_article_projection(
    *, profile: Path, source_assets: Sequence[Mapping[str, object]]
) -> str:
    """Require exact official parsed article identity/content → PostgreSQL rows."""

    expected_rows = _source_article_projection_records(
        profile=profile,
        source_assets=source_assets,
    )
    expected: dict[tuple[str, int, int | None, str], dict[str, object]] = {}
    for value in expected_rows:
        identity = (
            cast(str, value["law_id"]),
            cast(int, value["article_no"]),
            cast(int | None, value["paragraph_no"]),
            cast(str, value["version_key"]),
        )
        if identity in expected:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        expected[identity] = value
    if not expected:
        raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")

    from src.db.postgres import connect

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.law_id, a.article_no, a.paragraph_no, a.item_no,
                       a.title, a.content, a.content_hash, a.version_mst,
                       a.version_key, a.valid_from, a.valid_to,
                       parent.law_id, parent.article_no, parent.paragraph_no,
                       parent.version_mst, parent.version_key
                FROM law_articles a
                LEFT JOIN law_articles parent ON parent.id=a.parent_article_pk
                ORDER BY a.id
                """
            )
            database_rows = cur.fetchall()
    except Exception as exc:
        raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_UNAVAILABLE") from exc

    observed: dict[tuple[str, int, int | None, str], dict[str, object]] = {}
    for row in database_rows:
        if not isinstance(row, tuple) or len(row) != 16:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        (
            law_id,
            article_no,
            paragraph_no,
            item_no,
            title,
            content,
            content_hash,
            version_mst,
            version_key,
            valid_from,
            valid_to,
            parent_law_id,
            parent_article_no,
            parent_paragraph_no,
            parent_version_mst,
            parent_version_key,
        ) = row
        if (
            not isinstance(law_id, str)
            or law_id not in LAW_HISTORY_LAW_IDS
            or type(article_no) is not int
            or (paragraph_no is not None and type(paragraph_no) is not int)
            or item_no is not None
            or (title is not None and not isinstance(title, str))
            or not isinstance(content, str)
            or not isinstance(content_hash, str)
            or _SHA256.fullmatch(content_hash) is None
            or sha256_bytes(content.encode("utf-8")) != content_hash
            or not isinstance(version_mst, str)
            or _MST.fullmatch(version_mst) is None
            or not isinstance(version_key, str)
            or (version_match := _VERSION_KEY.fullmatch(version_key)) is None
            or version_match.group("mst") != version_mst
            or not isinstance(valid_from, date)
            or (valid_to is not None and not isinstance(valid_to, date))
            or (valid_to is not None and valid_to <= valid_from)
        ):
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        if parent_law_id is None:
            if any(
                value is not None
                for value in (
                    parent_article_no,
                    parent_paragraph_no,
                    parent_version_mst,
                    parent_version_key,
                )
            ):
                raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        elif (
            not isinstance(parent_law_id, str)
            or parent_law_id != law_id
            or type(parent_article_no) is not int
            or (parent_paragraph_no is not None and type(parent_paragraph_no) is not int)
            or not isinstance(parent_version_mst, str)
            or _MST.fullmatch(parent_version_mst) is None
            or not isinstance(parent_version_key, str)
            or (parent_match := _VERSION_KEY.fullmatch(parent_version_key)) is None
            or parent_match.group("mst") != parent_version_mst
        ):
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        identity = (law_id, article_no, paragraph_no, version_key)
        if identity in observed:
            raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
        observed[identity] = {
            "law_id": law_id,
            "article_no": article_no,
            "paragraph_no": paragraph_no,
            "version_mst": version_mst,
            "version_key": version_key,
            "title": title,
            "content_hash": content_hash,
            "valid_from": valid_from.isoformat(),
            "valid_to": valid_to.isoformat() if valid_to else None,
            "parent_law_id": parent_law_id,
            "parent_article_no": parent_article_no,
            "parent_paragraph_no": parent_paragraph_no,
            "parent_version_mst": parent_version_mst,
            "parent_version_key": parent_version_key,
        }
    if observed != expected:
        raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
    return sha256_bytes(canonical_json_bytes(list(expected_rows)))


def _record_refresh(
    *,
    refresh_id: str,
    from_date: date,
    to_date: date,
    refresh_sha256: str,
) -> None:
    """Record one explicit refresh without storing raw bodies or paths."""

    from src.db.postgres import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT from_date, to_date, refresh_sha256 FROM law_history_refreshes "
            "WHERE refresh_id=%s",
            (refresh_id,),
        )
        existing = cur.fetchone()
        if existing is not None:
            if existing != (from_date, to_date, refresh_sha256):
                raise LawHistoryError("LAW_HISTORY_REFRESH_ID_CONFLICT")
            return
        cur.execute(
            """
            INSERT INTO law_history_refreshes
              (refresh_id, profile, from_date, to_date, collected_at, refresh_sha256)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                refresh_id,
                LAW_HISTORY_PROFILE,
                from_date,
                to_date,
                datetime.now(timezone.utc),
                refresh_sha256,
            ),
        )


def _load_pending_refresh(profile: Path) -> dict[str, object] | None:
    path = _pending_refresh_path(profile)
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_UNSAFE")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID") from exc
    if not isinstance(value, dict):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    phase = value.get("phase")
    staged_keys = {
        "phase",
        "previous_manifest_sha256",
        "refresh",
        "source_assets",
    }
    ready_keys = {*staged_keys, "updated_manifest"}
    if (phase == "staged" and set(value) != staged_keys) or (
        phase == "ready" and set(value) != ready_keys
    ):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    return value


def _journal_refresh_sources(
    *,
    profile: Path,
    journal: Mapping[str, object],
    from_date: date,
    to_date: date,
) -> tuple[tuple[dict[str, object], ...], tuple[LawHistorySource, ...], dict[str, object]]:
    """Validate a staged refresh before it is allowed to mutate a store.

    The journal stores only profile-local receipt metadata and relative raw
    source names.  Re-reading and hashing every staged source makes an
    interrupted refresh resumable without trusting a partially completed DB
    transaction or collecting the official API a second time.
    """

    previous = journal.get("previous_manifest_sha256")
    refresh = journal.get("refresh")
    raw_assets = journal.get("source_assets")
    if (
        not isinstance(previous, str)
        or _SHA256.fullmatch(previous) is None
        or not isinstance(refresh, dict)
        or not isinstance(raw_assets, list)
        or not raw_assets
    ):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    assets: list[dict[str, object]] = []
    sources: list[LawHistorySource] = []
    seen: set[tuple[str, str]] = set()
    for raw_asset in raw_assets:
        if not isinstance(raw_asset, dict):
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
        _validate_source_asset(profile, raw_asset)
        if raw_asset.get("source_kind") != "refresh":
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
        source = _source_from_asset(raw_asset)
        key = (source.law_id, source.version_key)
        if key in seen:
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
        seen.add(key)
        assets.append(dict(raw_asset))
        sources.append(source)
    expected_refresh = _refresh_record(
        from_date=from_date,
        to_date=to_date,
        sources=tuple(sources),
    )
    if refresh != expected_refresh:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    return tuple(assets), tuple(sources), expected_refresh


def _combined_refresh_assets(
    *,
    manifest: Mapping[str, object],
    staged_assets: Sequence[Mapping[str, object]],
    profile: Path | None = None,
) -> tuple[list[dict[str, object]], tuple[dict[str, object], ...]]:
    """Merge immutable new versions while requiring duplicates to agree.

    A refresh deliberately re-collects the version effective at its end date
    for every law.  Most of those versions are already in the baseline or a
    prior refresh.  They prove coverage but must not overwrite the original
    receipt or create a second source provenance row.
    """

    raw_existing = manifest.get("source_assets")
    if not isinstance(raw_existing, list):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    combined: list[dict[str, object]] = []
    existing: dict[tuple[str, str], dict[str, object]] = {}
    for raw_asset in raw_existing:
        if not isinstance(raw_asset, dict):
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        key = (cast(str, raw_asset.get("law_id")), cast(str, raw_asset.get("version_key")))
        if key in existing:
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        existing[key] = dict(raw_asset)
        combined.append(dict(raw_asset))
    new_assets: list[dict[str, object]] = []
    for raw_asset in staged_assets:
        asset = dict(raw_asset)
        key = (cast(str, asset.get("law_id")), cast(str, asset.get("version_key")))
        current = existing.get(key)
        if current is None:
            existing[key] = asset
            combined.append(asset)
            new_assets.append(asset)
            continue
        if any(
            current.get(field) != asset.get(field)
            for field in ("effective_mst", "effective_date", "is_repealed")
        ):
            raise LawHistoryError("LAW_HISTORY_REFRESH_SOURCE_CONFLICT")
        if current.get("effective_sha256") != asset.get("effective_sha256"):
            # The official API can amend non-legal contact metadata without a
            # new MST or enforcement date.  Keep the original immutable
            # version receipt in the source ledger, but allow the refresh
            # receipt only when both hash-verified bodies project to exactly
            # the same legal metadata and articles.  Without a profile-local
            # body reader, a raw-hash variance remains fail-closed.
            if profile is None or (
                _source_receipt_content_sha256(profile, current)
                != _source_receipt_content_sha256(profile, asset)
            ):
                raise LawHistoryError("LAW_HISTORY_REFRESH_SOURCE_CONFLICT")
    return combined, tuple(new_assets)


def _updated_refresh_manifest(
    *,
    profile: Path | None = None,
    manifest: Mapping[str, object],
    staged_assets: Sequence[Mapping[str, object]],
    refresh: Mapping[str, object],
    to_date: date,
    counts: Mapping[str, int],
    source_article_projection_sha256: str,
    temporal_store_projection_sha256: str,
) -> dict[str, object]:
    """Construct one publishable manifest only after the stores verify."""

    combined_assets, _new_assets = _combined_refresh_assets(
        manifest=manifest,
        staged_assets=staged_assets,
        profile=profile,
    )
    raw_refreshes = manifest.get("refreshes")
    if not isinstance(raw_refreshes, list):
        raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
    refresh_with_receipts = {
        **dict(refresh),
        # ``source_assets`` retains one immutable canonical receipt per legal
        # version.  Refreshes additionally retain the exact local receipt
        # metadata used to prove their date coverage when an API response
        # changes only non-legal fields under the same MST/effective date.
        "receipt_assets": [dict(asset) for asset in staged_assets],
    }
    updated = dict(manifest)
    updated["coverage_through"] = to_date.isoformat()
    updated["source_assets"] = combined_assets
    updated["store_counts"] = dict(counts)
    updated["source_article_projection_sha256"] = source_article_projection_sha256
    updated["temporal_store_projection_sha256"] = temporal_store_projection_sha256
    updated["refreshes"] = [*raw_refreshes, refresh_with_receipts]
    # A benchmark is bound to one exact manifest/projection.  Any source or
    # coverage change invalidates it; a later explicit held-out run may bind
    # a new deployable strategy.
    updated["retrieval_strategy"] = "rrf_lexical"
    updated["retrieval_strategy_benchmark_sha256"] = None
    updated["data_build_timestamp"] = datetime.now(timezone.utc).isoformat()
    _validated_refresh_records(
        refreshes=updated["refreshes"],
        source_assets=updated["source_assets"],
        coverage_through=to_date,
    )
    return updated


def _validate_publishable_refresh_manifest(
    *,
    profile: Path,
    updated: Mapping[str, object],
) -> None:
    """Verify a ready journal without relying on its still-old active marker."""

    required = {
        "local_state_version",
        "profile",
        "coverage_from",
        "coverage_through",
        "law_ids",
        "source_assets",
        "model_ids/revisions",
        "store_counts",
        "source_article_projection_sha256",
        "temporal_store_projection_sha256",
        "frozen_inputs",
        "retrieval_strategy",
        "retrieval_strategy_benchmark_sha256",
        "refreshes",
        "data_build_timestamp",
    }
    if (
        set(updated) != required
        or updated.get("local_state_version") != LAW_HISTORY_LOCAL_STATE_VERSION
        or updated.get("profile") != LAW_HISTORY_PROFILE
        or updated.get("coverage_from") != LAW_HISTORY_BASELINE_AS_OF.isoformat()
        or updated.get("law_ids") != list(LAW_HISTORY_LAW_IDS)
        or updated.get("retrieval_strategy") not in {"rrf_lexical", "cross_encoder_rerank"}
        or updated.get("retrieval_strategy_benchmark_sha256") is not None
    ):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    try:
        coverage = date.fromisoformat(cast(str, updated["coverage_through"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID") from exc
    source_assets = updated.get("source_assets")
    if not isinstance(source_assets, list):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    refreshes = _validated_refresh_records(
        refreshes=updated.get("refreshes"),
        source_assets=source_assets,
        coverage_through=coverage,
    )
    for asset in source_assets:
        if not isinstance(asset, dict):
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
        _validate_source_asset(profile, asset)
    _validate_refresh_receipt_assets(
        profile=profile,
        refreshes=refreshes,
        source_assets=cast(Sequence[Mapping[str, object]], source_assets),
    )
    baseline_assets = [asset for asset in source_assets if asset.get("source_kind") == "baseline"]
    if (
        len(baseline_assets) != len(LAW_HISTORY_LAW_IDS)
        or {asset.get("law_id") for asset in baseline_assets} != set(LAW_HISTORY_LAW_IDS)
        or any(not _is_valid_baseline_asset(asset) for asset in baseline_assets)
    ):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    _validate_frozen_inputs_manifest(updated.get("frozen_inputs"))
    _validate_store_source_provenance(source_assets)
    source_projection = _verify_source_article_projection(
        profile=profile,
        source_assets=cast(Sequence[Mapping[str, object]], source_assets),
    )
    if updated.get("source_article_projection_sha256") != source_projection:
        raise LawHistoryError("LAW_HISTORY_SOURCE_ARTICLE_PROJECTION_MISMATCH")
    _validate_frozen_input_projections(cast(Mapping[str, object], updated["frozen_inputs"]))
    models = _law_history_model_inventory(profile=profile)
    if updated.get("model_ids/revisions") != models:
        raise LawHistoryError("LAW_HISTORY_MODEL_CACHE_MISMATCH")
    if updated.get("store_counts") != _history_store_counts():
        raise LawHistoryError("LAW_HISTORY_STORE_COUNT_MISMATCH")
    projection = _verify_temporal_store_projection()
    if updated.get("temporal_store_projection_sha256") != projection:
        raise LawHistoryError("LAW_HISTORY_TEMPORAL_STORE_MISMATCH")
    _validate_refresh_ledger(refreshes, allow_unrecorded_final=True)


def _validate_ready_refresh_manifest_binding(
    *,
    profile: Path,
    previous_manifest: Mapping[str, object],
    staged_assets: Sequence[Mapping[str, object]],
    refresh: Mapping[str, object],
    ready_manifest: Mapping[str, object],
) -> None:
    """Bind a ready journal to the exact staged receipts and prior manifest.

    A ready journal is durable across a process crash.  Before it may record a
    ledger row or replace the active marker, prove that its source inventory
    and final refresh entry are exactly the deterministic result of the staged
    receipt set.  This prevents a stale or edited ready file from affecting
    either persistent boundary.
    """

    previous_refreshes = previous_manifest.get("refreshes")
    if not isinstance(previous_refreshes, list) or any(
        not isinstance(item, dict) for item in previous_refreshes
    ):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    expected_source_assets, _new_assets = _combined_refresh_assets(
        manifest=previous_manifest,
        staged_assets=staged_assets,
        profile=profile,
    )
    expected_refresh = {
        **dict(refresh),
        "receipt_assets": [dict(asset) for asset in staged_assets],
    }
    if ready_manifest.get("source_assets") != expected_source_assets or ready_manifest.get(
        "refreshes"
    ) != [*previous_refreshes, expected_refresh]:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")


def _finish_pending_refresh(
    *, profile: Path, current_manifest_sha256: str, from_date: date, to_date: date
) -> bool:
    """Resume a staged refresh without re-collecting official source data.

    The durable journal is created *before* any PostgreSQL/Qdrant mutation.
    If a process dies during loading or indexing, the next invocation can
    validate the same local source receipts, finish the idempotent load, and
    only then atomically advance the manifest and refresh ledger together.
    """

    journal = _load_pending_refresh(profile)
    if journal is None:
        return False
    previous = journal.get("previous_manifest_sha256")
    if not isinstance(previous, str) or _SHA256.fullmatch(previous) is None:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    staged_assets, _sources, refresh = _journal_refresh_sources(
        profile=profile,
        journal=journal,
        from_date=from_date,
        to_date=to_date,
    )
    phase = cast(str, journal["phase"])
    if phase == "staged":
        if current_manifest_sha256 != previous:
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        manifest, active_digest = _load_active_manifest(profile)
        if active_digest != previous:
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        _combined_assets, new_assets = _combined_refresh_assets(
            manifest=manifest,
            staged_assets=staged_assets,
            profile=profile,
        )
        if new_assets:
            _load_history_source_assets(profile=profile, source_assets=new_assets)
            from src.ingest.laws import index_temporal_qdrant

            index_temporal_qdrant()
        counts = _history_store_counts()
        combined_assets, _new_assets = _combined_refresh_assets(
            manifest=manifest,
            staged_assets=staged_assets,
            profile=profile,
        )
        source_projection = _verify_source_article_projection(
            profile=profile,
            source_assets=combined_assets,
        )
        projection = _verify_temporal_store_projection()
        updated = _updated_refresh_manifest(
            profile=profile,
            manifest=manifest,
            staged_assets=staged_assets,
            refresh=refresh,
            to_date=to_date,
            counts=counts,
            source_article_projection_sha256=source_projection,
            temporal_store_projection_sha256=projection,
        )
        _write_json(
            _pending_refresh_path(profile),
            {
                "phase": "ready",
                "previous_manifest_sha256": previous,
                "refresh": refresh,
                "source_assets": list(staged_assets),
                "updated_manifest": updated,
            },
            replace=True,
        )
        journal = _load_pending_refresh(profile)
        assert journal is not None
        phase = cast(str, journal["phase"])
    if phase != "ready":
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    ready_manifest = journal.get("updated_manifest")
    if not isinstance(ready_manifest, dict):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    updated_digest = sha256_bytes(canonical_json_bytes(ready_manifest))
    if current_manifest_sha256 == previous:
        previous_manifest, active_digest = _load_active_manifest(profile)
        if active_digest != previous:
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        _validate_ready_refresh_manifest_binding(
            profile=profile,
            previous_manifest=previous_manifest,
            staged_assets=staged_assets,
            refresh=refresh,
            ready_manifest=ready_manifest,
        )
    elif current_manifest_sha256 == updated_digest:
        active_manifest, active_digest = _load_active_manifest(profile)
        if active_digest != updated_digest or active_manifest != ready_manifest:
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    else:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
    # Verify the ready journal and either the preceding or already-recorded
    # ledger chain *before* adding an idempotent ledger row.  This ordering
    # leaves malformed journals unable to mutate PostgreSQL.
    _validate_publishable_refresh_manifest(profile=profile, updated=ready_manifest)
    _record_refresh(
        refresh_id=cast(str, refresh["refresh_id"]),
        from_date=from_date,
        to_date=to_date,
        refresh_sha256=cast(str, refresh["source_sha256"]),
    )
    ready_refreshes = ready_manifest.get("refreshes")
    if not isinstance(ready_refreshes, list) or any(
        not isinstance(item, dict) for item in ready_refreshes
    ):
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")
    _validate_refresh_ledger(cast(Sequence[Mapping[str, object]], ready_refreshes))
    if current_manifest_sha256 == previous:
        _replace_active_manifest_if_digest(
            profile=profile,
            expected_sha256=previous,
            manifest=ready_manifest,
        )
    try:
        _pending_refresh_path(profile).unlink()
    except OSError as exc:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CLEANUP_FAILED") from exc
    return True


def refresh_law_history(
    *,
    from_date: date,
    to_date: date,
    local_root_override: Path | None = None,
    api_get: LawApiGet | None = None,
) -> Path:
    """Explicitly extend legal coverage; no scheduler or polling is used."""

    try:
        profile = ensure_functional_uat_law_history_profile(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    try:
        apply_functional_uat_law_history_runtime_environment(root=local_root_override)
    except LocalProfileError as exc:
        raise LawHistoryError("LAW_HISTORY_PROFILE_UNAVAILABLE") from exc
    with _law_history_mutation_lock(profile):
        # A staged range is the only recoverable interrupted state.  It must
        # be resumed before normal validation because the isolated stores may
        # correctly contain unmanifested source rows from the interrupted
        # first attempt.
        current_manifest, current_digest = _load_active_manifest(profile)
        if _finish_pending_refresh(
            profile=profile,
            current_manifest_sha256=current_digest,
            from_date=from_date,
            to_date=to_date,
        ):
            current_manifest, current_digest = _load_active_manifest(profile)
        # This verifies exact source receipts, model cache, stores and ledger.
        manifest, manifest_digest = validate_law_history_state(
            local_root_override=local_root_override
        )
        del current_manifest, current_digest
        try:
            coverage_through = date.fromisoformat(cast(str, manifest["coverage_through"]))
        except (TypeError, ValueError) as exc:
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID") from exc
        refreshes = manifest["refreshes"]
        if not isinstance(refreshes, list):
            raise LawHistoryError("LAW_HISTORY_MANIFEST_INVALID")
        same_range = [
            value
            for value in refreshes
            if isinstance(value, dict)
            and value.get("from") == from_date.isoformat()
            and value.get("to") == to_date.isoformat()
        ]
        if same_range:
            # Exact completed ranges are idempotent and must not issue a new
            # official-network collection.  State validation already proved
            # the matching ledger and all receipts are present.
            if len(same_range) != 1:
                raise LawHistoryError("LAW_HISTORY_REFRESH_CHAIN_INVALID")
            return _new_public_artifact(
                as_of=to_date,
                counts=cast(Mapping[str, int], manifest["store_counts"]),
                models=cast(Mapping[str, object], manifest["model_ids/revisions"]),
            )
        if from_date != coverage_through + timedelta(days=1) or to_date < from_date:
            raise LawHistoryError("LAW_HISTORY_REFRESH_RANGE_INVALID")
        try:
            validate_functional_uat_law_history_api_credential(root=local_root_override)
        except LocalProfileError as exc:
            raise LawHistoryError("LAW_HISTORY_CREDENTIAL_UNAVAILABLE") from exc
        resolved_api_get = api_get
        if resolved_api_get is None:
            from src import lawgo

            resolved_api_get = lawgo.get
        try:
            with functional_uat_law_history_collection_environment(root=local_root_override):
                source_root, sources = collect_law_history_refresh(
                    profile=profile,
                    from_date=from_date,
                    to_date=to_date,
                    api_get=resolved_api_get,
                )
        except LocalProfileError as exc:
            raise LawHistoryError("LAW_HISTORY_CREDENTIAL_UNAVAILABLE") from exc
        staged_assets = [
            source.local_state(profile=profile, source_root=source_root) for source in sources
        ]
        refresh_record = _refresh_record(from_date=from_date, to_date=to_date, sources=sources)
        pending = _pending_refresh_path(profile)
        if pending.exists() or pending.is_symlink():
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        # This is the durable boundary: no source/store mutation happens
        # before all receipt identity needed to retry is atomically staged.
        _write_json(
            pending,
            {
                "phase": "staged",
                "previous_manifest_sha256": manifest_digest,
                "refresh": refresh_record,
                "source_assets": staged_assets,
            },
        )
        if not _finish_pending_refresh(
            profile=profile,
            current_manifest_sha256=manifest_digest,
            from_date=from_date,
            to_date=to_date,
        ):
            raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")
        final_manifest, _final_digest = validate_law_history_state(
            local_root_override=local_root_override
        )
        return _new_public_artifact(
            as_of=to_date,
            counts=cast(Mapping[str, int], final_manifest["store_counts"]),
            models=cast(Mapping[str, object], final_manifest["model_ids/revisions"]),
        )


class FunctionalLawHistoryEvidenceBackend(DatabaseEvidenceBackend):
    """Evidence backend that routes legal search to the temporal collection."""

    def __init__(
        self,
        *,
        coverage_from: date,
        coverage_through: date,
        use_rerank: bool = False,
        rerank_device: Literal["cpu", "cuda"] = "cpu",
        archive_catalog: ActiveArchiveCatalog | None = None,
    ) -> None:
        if coverage_from != LAW_HISTORY_BASELINE_AS_OF or coverage_through < coverage_from:
            raise LawHistoryError("LAW_HISTORY_COVERAGE_RANGE_INVALID")
        super().__init__()
        self._coverage_from = coverage_from
        self._coverage_through = coverage_through
        self._use_rerank = use_rerank
        self._rerank_device = rerank_device
        # ``None`` retains direct/unit caller compatibility with the earlier
        # fixed-input profile.  The actual service always captures a catalog,
        # including an explicit empty catalog, and therefore fails closed for
        # HSK/CCCT dates without an activated official archive.
        self._archive_catalog = archive_catalog

    def snapshot_effective_date(self, _snapshot_id: str) -> date:
        # HSK and CCCT are deliberately retained at the baseline date.
        return LAW_HISTORY_BASELINE_AS_OF

    def temporal_evidence_support(
        self, _snapshot_id: str | None, review_as_of: date
    ) -> TemporalEvidenceSupport:
        law_covered = self._coverage_from <= review_as_of <= self._coverage_through
        archive = self._archive_catalog.for_date(review_as_of) if self._archive_catalog else None
        if self._archive_catalog is None or review_as_of == LAW_HISTORY_BASELINE_AS_OF:
            hsk_as_of = LAW_HISTORY_BASELINE_AS_OF
            requirements_as_of = LAW_HISTORY_BASELINE_AS_OF
            hsk_covered = True
            requirements_covered = True
            hsk_snapshot_id = None
            requirements_snapshot_id = None
        elif archive is not None:
            hsk_as_of = archive.archive_as_of
            requirements_as_of = archive.archive_as_of
            hsk_covered = True
            requirements_covered = True
            hsk_snapshot_id = archive.hsk_snapshot_id
            requirements_snapshot_id = archive.ccct.snapshot_id
        else:
            # Keep the previous known baseline date explicit, but never use it
            # as a substitute for the requested archive date.
            hsk_as_of = LAW_HISTORY_BASELINE_AS_OF
            requirements_as_of = LAW_HISTORY_BASELINE_AS_OF
            hsk_covered = False
            requirements_covered = False
            hsk_snapshot_id = None
            requirements_snapshot_id = None
        return TemporalEvidenceSupport(
            context=TemporalContext(
                law_as_of=review_as_of,
                hsk_snapshot_as_of=hsk_as_of,
                requirements_snapshot_as_of=requirements_as_of,
            ),
            law_coverage_available=law_covered,
            requirements_lookup_as_of=requirements_as_of,
            temporal_coverage=TemporalCoverage(
                law="covered" if law_covered else "unavailable",
                hsk="covered" if hsk_covered else "unavailable",
                requirements="covered" if requirements_covered else "unavailable",
            ),
            hsk_coverage_available=hsk_covered,
            requirements_coverage_available=requirements_covered,
            hsk_snapshot_id=hsk_snapshot_id,
            requirements_snapshot_id=requirements_snapshot_id,
        )

    def resolve_hsk_as_of(self, code: str | None, review_as_of: date) -> repository.HskResolution:
        """Resolve only against the selected archive; never substitute current HSK."""

        if self._archive_catalog is None or review_as_of == LAW_HISTORY_BASELINE_AS_OF:
            return super().resolve_hsk(code)
        archive = self._archive_catalog.for_date(review_as_of)
        if archive is None:
            raise LawHistoryError("HSK_CCCT_ARCHIVE_COVERAGE_MISSING")
        from src.hsk_ccct_history import resolve_hsk_snapshot

        return resolve_hsk_snapshot(archive.hsk_snapshot_id, code)

    def search_law_articles(
        self, query: str, top_k: int, as_of: date
    ) -> tuple[LawArticleMatch, ...]:
        """Temporal-filter each arm before RRF; no current graph expansion."""

        recall = min(max(top_k * 2, 8), 40)
        dense_hits = repository.search(
            query,
            arm="dense",
            limit=recall,
            as_of=as_of,
            collection=repository.TEMPORAL_COLLECTION,
            temporal=True,
        )
        bm25_hits = repository.search(
            query,
            arm="bm25",
            limit=recall,
            as_of=as_of,
            collection=repository.TEMPORAL_COLLECTION,
            temporal=True,
        )
        candidates: dict[int, dict[str, object]] = {}
        rrf_k = 60
        for method, hits in (("dense", dense_hits), ("bm25", bm25_hits)):
            for rank, hit in enumerate(hits, start=1):
                entry = candidates.setdefault(
                    hit.article_pk, {"methods": set(), "raw": {}, "rrf": 0.0}
                )
                cast(set[str], entry["methods"]).add(method)
                cast(dict[str, float], entry["raw"])[method] = float(hit.score)
                entry["rrf"] = cast(float, entry["rrf"]) + 1.0 / (rrf_k + rank)
        if not candidates:
            return ()
        from psycopg.rows import dict_row

        from src.db.postgres import connect

        with connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT a.id AS article_pk, a.law_id, version.law_name, version.hierarchy,
                       a.article_no, a.paragraph_no, a.item_no, a.title, a.content,
                       a.content_hash, a.version_mst, a.valid_from, a.valid_to,
                       source.official_uri
                FROM law_articles a
                JOIN law_versions version
                  ON version.law_id=a.law_id AND version.version_key=a.version_key
                JOIN law_version_sources source
                  ON source.law_id=a.law_id AND source.version_key=a.version_key
                WHERE a.id = ANY(%s) AND a.valid_from <= %s
                  AND (a.valid_to IS NULL OR a.valid_to > %s)
                """,
                (list(candidates), as_of, as_of),
            )
            rows = {int(row["article_pk"]): dict(row) for row in cur.fetchall()}
        import re as _re

        query_tokens = set(_re.findall(r"[0-9A-Za-z가-힣]{2,}", query.lower()))
        maximum = 2.0 / (rrf_k + 1)
        matches: list[LawArticleMatch] = []
        for article_pk, candidate in candidates.items():
            row = rows.get(article_pk)
            if row is None:
                continue
            text_tokens = set(_re.findall(r"[0-9A-Za-z가-힣]{2,}", row["content"].lower()))
            lexical = len(query_tokens & text_tokens) / len(query_tokens) if query_tokens else 0.0
            rrf = cast(float, candidate["rrf"])
            matches.append(
                LawArticleMatch(
                    row=row,
                    retrieval_methods=cast(
                        tuple[Literal["dense", "bm25", "graph", "rerank"], ...],
                        tuple(
                            method
                            for method in ("dense", "bm25")
                            if method in cast(set[str], candidate["methods"])
                        ),
                    ),
                    score=min(1.0, 0.85 * min(1.0, rrf / maximum) + 0.15 * lexical),
                    raw_scores={
                        **cast(dict[str, float], candidate["raw"]),
                        "rrf": rrf,
                        "lexical_overlap": float(lexical),
                    },
                )
            )
        matches.sort(key=lambda value: (-value.score, int(value.row["article_pk"])))
        if self._use_rerank and matches:
            # The local pinned model is optional at inference time.  A load or
            # inference failure leaves the deterministic RRF+lexical ranking
            # unchanged and does not claim ``rerank`` in the public evidence.
            from src.embed import rerank

            scores = rerank.scores(
                query,
                [match.row["content"][:2000] for match in matches],
                device=self._rerank_device,
            )
            if scores is not None and len(scores) == len(matches):
                reranked: list[LawArticleMatch] = []
                for match, score in zip(matches, scores):
                    if not 0.0 <= float(score) <= 1.0:
                        reranked = []
                        break
                    reranked.append(
                        LawArticleMatch(
                            row=match.row,
                            retrieval_methods=(*match.retrieval_methods, "rerank"),
                            score=float(score),
                            raw_scores={**match.raw_scores, "rerank": float(score)},
                        )
                    )
                if reranked:
                    reranked.sort(key=lambda value: (-value.score, int(value.row["article_pk"])))
                    return tuple(reranked[:top_k])
        return tuple(matches[:top_k])


def backend_from_active_law_history(
    *, local_root_override: Path | None = None
) -> FunctionalLawHistoryEvidenceBackend:
    manifest, _digest = validate_law_history_state(local_root_override=local_root_override)
    from src.hsk_ccct_history import capture_active_archive_catalog

    return FunctionalLawHistoryEvidenceBackend(
        coverage_from=LAW_HISTORY_BASELINE_AS_OF,
        coverage_through=date.fromisoformat(cast(str, manifest["coverage_through"])),
        use_rerank=manifest["retrieval_strategy"] == "cross_encoder_rerank",
        archive_catalog=capture_active_archive_catalog(
            local_root_override=local_root_override,
            verify_assets=True,
        ),
    )


__all__ = [
    "LAW_HISTORY_BASELINE_AS_OF",
    "LAW_HISTORY_LOCAL_STATE_VERSION",
    "LAW_HISTORY_PROFILE",
    "FunctionalLawHistoryEvidenceBackend",
    "LawHistoryError",
    "LawHistorySource",
    "backend_from_active_law_history",
    "bind_law_history_retrieval_strategy",
    "build_frozen_input_attestation",
    "build_law_history",
    "collect_law_history_baseline",
    "collect_law_history_refresh",
    "discard_active_law_history_manifest",
    "recover_law_history_baseline",
    "recover_law_history_build_lock",
    "recover_law_history_published_lock",
    "recover_law_history_refresh_lock",
    "rebind_law_history_frozen_inputs",
    "refresh_law_history",
    "validate_law_history_state",
    "verify_law_history_store",
]
