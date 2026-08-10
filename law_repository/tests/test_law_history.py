"""Pure law-history profile boundaries; no Law API or local stores required."""

from __future__ import annotations

from datetime import date
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.law_history import (
    LAW_HISTORY_BASELINE_AS_OF,
    LAW_HISTORY_PROFILE,
    FunctionalLawHistoryEvidenceBackend,
    LawHistoryError,
    LawHistorySource,
    _candidate_as_of,
    _catalog_for_law,
    _journal_refresh_sources,
    _is_valid_baseline_asset,
    _profile_source_file,
    _refresh_record,
    _source_article_projection_records,
    _updated_refresh_manifest,
    _validated_refresh_records,
    collect_law_history_baseline,
    collect_law_history_refresh,
    _validate_source_asset,
    _verify_temporal_store_projection,
    _verify_source_article_projection,
    _validate_store_source_provenance,
)
from src.law_history_spec import LAW_HISTORY_LAW_IDS, LAW_HISTORY_LAWS


class _HistoryCursor:
    """Small DB-free cursor for the append-only interval policy."""

    def __init__(self, *, open_rows: list[tuple[object, ...]]) -> None:
        self.open_rows = open_rows
        self.one: tuple[object, ...] | None = None
        self.updates: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        normalized = " ".join(query.split())
        if normalized.startswith("SELECT 1 FROM law_versions"):
            self.one = None
        elif normalized.startswith("SELECT max(enforcement_date)"):
            self.one = (date(2026, 8, 7),)
        elif normalized.startswith("SELECT id, article_no"):
            self.one = None
        elif normalized.startswith("UPDATE law_articles"):
            self.updates.append((normalized, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.one

    def fetchall(self) -> list[tuple[object, ...]]:
        return self.open_rows


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _complete_refresh_sources(
    *, effective_date: date, digest: str, mst_offset: int
) -> tuple[LawHistorySource, ...]:
    """Build receipt-only sources for all fixed registry laws in pure tests."""

    return tuple(
        LawHistorySource(
            law_id=law_id,
            current_mst=str(mst_offset + index),
            current_file="unused.json",
            current_sha256=digest,
            effective_mst=str(mst_offset + index),
            effective_file="unused.json",
            effective_sha256=digest,
            effective_date=effective_date,
            article_count=1,
            is_repealed=False,
            law_name=law_name,
            hierarchy="법률",
            ministry="",
            official_uri=f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={mst_offset + index}",
            collected_at=datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
            source_kind="refresh",
        )
        for index, (law_id, law_name) in enumerate(LAW_HISTORY_LAWS)
    )


def test_candidate_registry_is_fixed_to_65_unique_official_law_ids() -> None:
    assert LAW_HISTORY_PROFILE == "functional-uat-law-history"
    assert LAW_HISTORY_BASELINE_AS_OF == date(2026, 8, 5)
    assert len(LAW_HISTORY_LAWS) == 65
    assert len(LAW_HISTORY_LAW_IDS) == 65
    assert len(set(LAW_HISTORY_LAW_IDS)) == 65


def test_law_history_frozen_hsk_and_ccct_inputs_are_attested_before_projection() -> None:
    from src.law_history import build_frozen_input_attestation

    attestation = build_frozen_input_attestation().manifest
    assert attestation["hsk_snapshot_as_of"] == "2026-08-05"
    assert attestation["requirements_snapshot_as_of"] == "2026-08-05"
    assert attestation["hsk"]["node_count"] == 17_072
    assert attestation["hsk"]["leaf_count"] == 11_327
    assert attestation["ccct"]["query_count"] == 22_654
    assert attestation["ccct"]["raw_row_count"] == 14_726


def test_baseline_asset_uses_effective_on_or_before_snapshot_date() -> None:
    """The snapshot date is a selection boundary, not every law's 시행일."""

    base = {"is_repealed": False, "effective_date": "2026-07-01"}
    assert _is_valid_baseline_asset(base) is True
    assert _is_valid_baseline_asset({**base, "effective_date": "2026-08-05"}) is True
    assert _is_valid_baseline_asset({**base, "effective_date": "2026-08-06"}) is False
    assert _is_valid_baseline_asset({**base, "is_repealed": True}) is False


def test_effective_catalog_reads_all_pages_and_selects_exact_as_of_version() -> None:
    calls: list[dict[str, object]] = []

    def api_get(_endpoint: str, **params: object) -> dict:
        calls.append(params)
        if params["nw"] != "1":
            return {"LawSearch": {"totalCnt": "0", "law": []}}
        if params["page"] == 1:
            return {
                "LawSearch": {
                    "totalCnt": "2",
                    "law": [
                        {
                            "법령ID": "001556",
                            "법령일련번호": "100",
                            "시행일자": "20260805",
                        }
                    ],
                }
            }
        return {
            "LawSearch": {
                "totalCnt": "2",
                "law": [
                    {
                        "법령ID": "001556",
                        "법령일련번호": "101",
                        "시행일자": "20260809",
                    }
                ],
            }
        }

    catalog = _catalog_for_law(law_id="001556", api_get=api_get)
    assert [(params["nw"], params["page"]) for params in calls] == [
        ("1", 1),
        ("1", 2),
        ("2", 1),
        ("3", 1),
    ]
    assert all(params["target"] == "eflaw" for params in calls)
    assert all(params["LID"] == "001556" for params in calls)
    assert {params["nw"] for params in calls} == {"1", "2", "3"}
    assert [candidate.mst for candidate in catalog] == ["100", "101"]
    assert _candidate_as_of(catalog, as_of=date(2026, 8, 8)).mst == "100"

    with pytest.raises(LawHistoryError, match="CATALOG_PAGE_INCOMPLETE"):
        _catalog_for_law(
            law_id="001556",
            api_get=lambda _endpoint, **_params: {
                "LawSearch": {
                    "totalCnt": "101",
                    "law": [
                        {
                            "법령ID": "001556",
                            "법령일련번호": str(index),
                            "시행일자": "20260805",
                        }
                        for index in range(100)
                    ],
                }
            },
        )


def test_baseline_and_refresh_fetch_exact_effective_body_not_current_substitute(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import src.law_history as history

    monkeypatch.setattr(history, "LAW_HISTORY_LAW_IDS", ("001556",))
    calls: list[dict[str, object]] = []

    def body(mst: str, effective: str) -> dict:
        return {
            "법령": {
                "기본정보": {
                    "법령ID": "001556",
                    "법령명_한글": "관세법",
                    "시행일자": effective,
                    "공포일자": "20260801",
                    "제개정구분": "일부개정",
                    "소관부처": "관세청",
                },
                "조문": {
                    "조문단위": {
                        "조문여부": "조문",
                        "조문번호": "0001",
                        "조문가지번호": "00",
                        "조문제목": "목적",
                        "조문내용": f"검증용 조문 {mst}",
                    }
                },
            }
        }

    def api_get(endpoint: str, **params: object) -> dict:
        calls.append({"endpoint": endpoint, **params})
        if endpoint == "lawSearch.do":
            if params["nw"] == "1":
                return {
                    "LawSearch": {
                        "totalCnt": "2",
                        "law": [
                            {
                                "법령ID": "001556",
                                "법령일련번호": "100",
                                "시행일자": "20260805",
                            },
                            {
                                "법령ID": "001556",
                                "법령일련번호": "101",
                                "시행일자": "20260806",
                            },
                        ],
                    }
                }
            return {"LawSearch": {"totalCnt": "0", "law": []}}
        assert endpoint == "lawService.do"
        assert params["target"] == "eflaw"
        assert params["MST"] in {"100", "101"}
        return body(str(params["MST"]), str(params["efYd"]))

    profile = tmp_path / "profile"
    profile.mkdir()
    _root, baseline = collect_law_history_baseline(
        profile=profile, as_of=date(2026, 8, 5), api_get=api_get
    )
    _refresh_root, refresh = collect_law_history_refresh(
        profile=profile,
        from_date=date(2026, 8, 6),
        to_date=date(2026, 8, 6),
        api_get=api_get,
    )

    assert [(source.effective_mst, source.effective_date) for source in baseline] == [
        ("100", date(2026, 8, 5))
    ]
    assert [(source.effective_mst, source.effective_date) for source in refresh] == [
        ("101", date(2026, 8, 6))
    ]
    law_service = [call for call in calls if call["endpoint"] == "lawService.do"]
    assert {call["efYd"] for call in law_service} == {"20260805", "20260806"}


def test_temporal_backend_exposes_fixed_hsk_and_requirement_baseline() -> None:
    backend = FunctionalLawHistoryEvidenceBackend(
        coverage_from=date(2026, 8, 5), coverage_through=date(2026, 8, 10)
    )
    covered = backend.temporal_evidence_support("snapshot", date(2026, 8, 8))
    assert covered.law_coverage_available is True
    assert covered.context.law_as_of == date(2026, 8, 8)
    assert covered.context.hsk_snapshot_as_of == date(2026, 8, 5)
    assert covered.requirements_lookup_as_of == date(2026, 8, 5)

    missing = backend.temporal_evidence_support("snapshot", date(2026, 8, 11))
    assert missing.law_coverage_available is False


def test_law_history_rejects_coverage_before_the_promised_baseline() -> None:
    with pytest.raises(LawHistoryError, match="COVERAGE_RANGE_INVALID"):
        FunctionalLawHistoryEvidenceBackend(
            coverage_from=date(2026, 8, 4), coverage_through=date(2026, 8, 5)
        )


def test_temporal_vector_collection_cannot_be_searched_without_date_filter() -> None:
    from src import repository

    with pytest.raises(ValueError, match="temporal collection requires"):
        repository.search("검증", collection=repository.TEMPORAL_COLLECTION)


def test_refresh_chain_is_contiguous_and_receipt_bound_to_manifest_assets(tmp_path) -> None:
    sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 6), digest="a" * 64, mst_offset=10_000
    )
    record = _refresh_record(from_date=date(2026, 8, 6), to_date=date(2026, 8, 6), sources=sources)
    assets = [source.local_state(profile=tmp_path, source_root=tmp_path) for source in sources]
    assert _validated_refresh_records(
        refreshes=[record], source_assets=assets, coverage_through=date(2026, 8, 6)
    ) == (record,)

    broken = dict(record)
    broken["from"] = "2026-08-07"
    with pytest.raises(LawHistoryError, match="REFRESH_CHAIN_INVALID"):
        _validated_refresh_records(
            refreshes=[broken], source_assets=assets, coverage_through=date(2026, 8, 6)
        )


def test_staged_refresh_receipt_is_revalidated_before_any_resume_mutation(tmp_path) -> None:
    """A retry can use only the exact local raw receipt staged before loading."""

    profile = tmp_path / "profile"
    source_root = profile / "data" / "law-history" / "refresh"
    source_root.mkdir(parents=True)
    body = {
        "법령": {
            "기본정보": {
                "법령ID": "001556",
                "법령명_한글": "관세법",
                "시행일자": "20260806",
                "공포일자": "20260801",
                "제개정구분": "일부개정",
                "소관부처": "관세청",
            },
            "조문": {
                "조문단위": {
                    "조문여부": "조문",
                    "조문번호": "0001",
                    "조문가지번호": "00",
                    "조문제목": "목적",
                    "조문내용": "재개 검증 조문",
                }
            },
        }
    }
    raw = source_root / "law.json"
    raw.write_text(json.dumps(body), encoding="utf-8")
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    source = LawHistorySource(
        law_id="001556",
        current_mst="101",
        current_file="law.json",
        current_sha256=digest,
        effective_mst="101",
        effective_file="law.json",
        effective_sha256=digest,
        effective_date=date(2026, 8, 6),
        article_count=1,
        is_repealed=False,
        law_name="관세법",
        hierarchy="법률",
        ministry="관세청",
        official_uri="https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=101",
        collected_at=datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
        source_kind="refresh",
    )
    asset = source.local_state(profile=profile, source_root=source_root)
    refresh = _refresh_record(
        from_date=date(2026, 8, 6), to_date=date(2026, 8, 6), sources=(source,)
    )

    assets, sources, parsed = _journal_refresh_sources(
        profile=profile,
        journal={
            "phase": "staged",
            "previous_manifest_sha256": "a" * 64,
            "refresh": refresh,
            "source_assets": [asset],
        },
        from_date=date(2026, 8, 6),
        to_date=date(2026, 8, 6),
    )

    assert assets == (asset,)
    assert [(value.law_id, value.effective_mst, value.effective_sha256) for value in sources] == [
        (source.law_id, source.effective_mst, source.effective_sha256)
    ]
    assert parsed == refresh


def test_refresh_invalidates_a_pre_refresh_benchmark_binding() -> None:
    baseline_sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 5), digest="a" * 64, mst_offset=10_000
    )
    refresh_sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 6), digest="b" * 64, mst_offset=20_000
    )
    baseline_assets = [
        {
            "law_id": source.law_id,
            "effective_mst": source.effective_mst,
            "effective_date": source.effective_date.isoformat(),
            "effective_sha256": source.effective_sha256,
            "is_repealed": source.is_repealed,
        }
        for source in baseline_sources
    ]
    staged_assets = [
        {
            "law_id": source.law_id,
            "effective_mst": source.effective_mst,
            "effective_date": source.effective_date.isoformat(),
            "effective_sha256": source.effective_sha256,
            "is_repealed": source.is_repealed,
        }
        for source in refresh_sources
    ]
    refresh = _refresh_record(
        from_date=date(2026, 8, 6), to_date=date(2026, 8, 6), sources=refresh_sources
    )

    updated = _updated_refresh_manifest(
        manifest={
            "source_assets": baseline_assets,
            "refreshes": [],
            "retrieval_strategy": "cross_encoder_rerank",
            "retrieval_strategy_benchmark_sha256": "c" * 64,
        },
        staged_assets=staged_assets,
        refresh=refresh,
        to_date=date(2026, 8, 6),
        counts={"law_count": 65},
        source_article_projection_sha256="e" * 64,
        temporal_store_projection_sha256="d" * 64,
    )

    assert updated["retrieval_strategy"] == "rrf_lexical"
    assert updated["retrieval_strategy_benchmark_sha256"] is None
    assert updated["coverage_through"] == "2026-08-06"
    assert len(updated["source_assets"]) == 130


def test_refresh_receipt_requires_an_as_of_selection_for_every_registry_law() -> None:
    """A subset journal can never advance coverage_through for all 65 laws."""

    all_sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 6), digest="a" * 64, mst_offset=10_000
    )
    subset = all_sources[:-1]
    record = _refresh_record(from_date=date(2026, 8, 6), to_date=date(2026, 8, 6), sources=subset)
    assets = [
        {
            "law_id": source.law_id,
            "effective_mst": source.effective_mst,
            "effective_date": source.effective_date.isoformat(),
            "effective_sha256": source.effective_sha256,
            "is_repealed": source.is_repealed,
        }
        for source in subset
    ]

    with pytest.raises(LawHistoryError, match="REFRESH_CHAIN_INVALID"):
        _validated_refresh_records(
            refreshes=[record],
            source_assets=assets,
            coverage_through=date(2026, 8, 6),
        )


def test_law_history_baseline_requires_dense_but_not_optional_reranker(tmp_path) -> None:
    from src.functional_uat import prefetch_models
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    calls: list[dict[str, object]] = []

    def downloader(**kwargs: object) -> str:
        calls.append(kwargs)
        return str(tmp_path / "cache")

    inventory = prefetch_models(
        profile_directory=profile,
        downloader=downloader,
        include_reranker=False,
    )
    assert set(inventory) == {"dense"}
    assert len(calls) == 1
    assert calls[0]["repo_id"] == inventory["dense"]["model_id"]


def test_law_history_source_path_rejects_windows_and_parent_escapes(tmp_path) -> None:
    profile = tmp_path / "profile"
    source = profile / "data" / "law-history" / "run" / "source.json"
    source.parent.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")

    assert _profile_source_file(profile, "data/law-history/run/source.json") == source
    for unsafe in ("../outside.json", r"\outside.json", r"C:\outside.json"):
        with pytest.raises(LawHistoryError, match="SOURCE_PATH_INVALID"):
            _profile_source_file(profile, unsafe)


def test_refresh_recovery_releases_only_a_verified_dead_owner_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A crash can resume, but no stale lock is ever removed implicitly."""

    from src import law_history as history
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    lock = profile / "state" / ".law-history-mutation.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps(
            {
                "profile": LAW_HISTORY_PROFILE,
                "pid": 123456,
                "created_at": "2026-08-10T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    journal = {
        "phase": "staged",
        "previous_manifest_sha256": "a" * 64,
        "refresh": {},
        "source_assets": [{}],
    }
    monkeypatch.setattr(history, "_load_pending_refresh", lambda _profile: journal)
    monkeypatch.setattr(
        history,
        "_journal_refresh_sources",
        lambda **_kwargs: ((), (), {}),
    )
    monkeypatch.setattr(history, "_load_active_manifest", lambda _profile: ({}, "a" * 64))
    monkeypatch.setattr(history, "_mutation_owner_is_live", lambda _pid: False)

    assert (
        history.recover_law_history_refresh_lock(
            from_date=date(2026, 8, 6),
            to_date=date(2026, 8, 6),
            confirm_profile=LAW_HISTORY_PROFILE,
            local_root_override=tmp_path,
        )
        is True
    )
    assert not lock.exists()


def test_refresh_recovery_refuses_to_remove_a_live_or_unverified_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from src import law_history as history
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    lock = profile / "state" / ".law-history-mutation.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps(
            {
                "profile": LAW_HISTORY_PROFILE,
                "pid": 123456,
                "created_at": "2026-08-10T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "_mutation_owner_is_live", lambda _pid: True)

    with pytest.raises(LawHistoryError, match="MUTATION_LOCK_ACTIVE"):
        history.recover_law_history_refresh_lock(
            from_date=date(2026, 8, 6),
            to_date=date(2026, 8, 6),
            confirm_profile=LAW_HISTORY_PROFILE,
            local_root_override=tmp_path,
        )
    assert lock.exists()


def test_build_recovery_releases_only_a_verified_dead_lock_without_active_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An interrupted baseline needs an explicit reset, never silent reuse."""

    from src import law_history as history
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    lock = profile / "state" / ".law-history-mutation.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps(
            {
                "profile": LAW_HISTORY_PROFILE,
                "pid": 123456,
                "created_at": "2026-08-10T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(history, "_mutation_owner_is_live", lambda _pid: False)

    assert (
        history.recover_law_history_build_lock(
            confirm_profile=LAW_HISTORY_PROFILE,
            local_root_override=tmp_path,
        )
        is True
    )
    assert not lock.exists()


def test_build_recovery_refuses_when_an_active_manifest_or_journal_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from src import law_history as history
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    state = profile / "state"
    state.mkdir(parents=True, exist_ok=True)
    lock = state / ".law-history-mutation.lock"
    lock.write_text(
        json.dumps(
            {
                "profile": LAW_HISTORY_PROFILE,
                "pid": 123456,
                "created_at": "2026-08-10T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (state / "active-law-history-manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(history, "_mutation_owner_is_live", lambda _pid: False)

    with pytest.raises(LawHistoryError, match="BUILD_RECOVERY_ACTIVE_MANIFEST"):
        history.recover_law_history_build_lock(
            confirm_profile=LAW_HISTORY_PROFILE,
            local_root_override=tmp_path,
        )
    assert lock.exists()


def test_published_recovery_requires_a_verified_stable_active_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from src import law_history as history
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    state = profile / "state"
    state.mkdir(parents=True, exist_ok=True)
    lock = state / ".law-history-mutation.lock"
    lock.write_text(
        json.dumps(
            {
                "profile": LAW_HISTORY_PROFILE,
                "pid": 123456,
                "created_at": "2026-08-10T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (state / "active-law-history-manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(history, "_mutation_owner_is_live", lambda _pid: False)
    monkeypatch.setattr(
        history,
        "validate_law_history_state",
        lambda **_kwargs: ({}, "b" * 64),
    )
    monkeypatch.setattr(
        history,
        "verify_law_history_store",
        lambda **_kwargs: {"manifest_sha256": "b" * 64},
    )

    assert (
        history.recover_law_history_published_lock(
            confirm_profile=LAW_HISTORY_PROFILE,
            local_root_override=tmp_path,
        )
        is True
    )
    assert not lock.exists()


def test_published_recovery_refuses_unverified_manifest_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from src import law_history as history
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    state = profile / "state"
    state.mkdir(parents=True, exist_ok=True)
    lock = state / ".law-history-mutation.lock"
    lock.write_text(
        json.dumps(
            {
                "profile": LAW_HISTORY_PROFILE,
                "pid": 123456,
                "created_at": "2026-08-10T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (state / "active-law-history-manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(history, "_mutation_owner_is_live", lambda _pid: False)
    monkeypatch.setattr(
        history,
        "validate_law_history_state",
        lambda **_kwargs: (_ for _ in ()).throw(
            LawHistoryError("LAW_HISTORY_STORE_COUNT_MISMATCH")
        ),
    )

    with pytest.raises(LawHistoryError, match="STORE_COUNT_MISMATCH"):
        history.recover_law_history_published_lock(
            confirm_profile=LAW_HISTORY_PROFILE,
            local_root_override=tmp_path,
        )
    assert lock.exists()


def test_law_history_source_asset_requires_exact_effective_uri_and_dates(tmp_path) -> None:
    profile = tmp_path / "profile"
    raw = profile / "data" / "law-history" / "run" / "law.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(
        """
        {
          "법령": {
            "기본정보": {
              "법령ID": "001556",
              "법령명_한글": "관세법",
              "시행일자": "20260805",
              "공포일자": "20260801",
              "제개정구분": "일부개정",
              "소관부처": "관세청"
            },
            "조문": {
              "조문단위": {
                "조문여부": "조문",
                "조문번호": "0001",
                "조문가지번호": "00",
                "조문제목": "목적",
                "조문내용": "검증용 조문"
              }
            }
          }
        }
        """,
        encoding="utf-8",
    )
    digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    asset = {
        "law_id": "001556",
        "current_mst": "100",
        "current_file": "data/law-history/run/law.json",
        "current_sha256": digest,
        "effective_mst": "100",
        "effective_file": "data/law-history/run/law.json",
        "effective_sha256": digest,
        "effective_date": "2026-08-05",
        "article_count": 1,
        "is_repealed": False,
        "law_name": "관세법",
        "hierarchy": "법률",
        "ministry": "관세청",
        "official_uri": "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=100",
        "collected_at": datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
        "source_kind": "baseline",
    }
    _validate_source_asset(profile, asset)

    asset["official_uri"] = "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=999"
    with pytest.raises(LawHistoryError, match="MANIFEST_INVALID"):
        _validate_source_asset(profile, asset)


def test_full_source_article_projection_binds_raw_body_title_content_and_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A full audit rejects DB prose/title drift even when hashes/counts look valid."""

    profile = tmp_path / "profile"
    source_root = profile / "data" / "law-history" / "run"
    source_root.mkdir(parents=True)

    def write_body(filename: str, *, mst: str, effective: str, text: str) -> tuple[Path, str]:
        path = source_root / filename
        path.write_text(
            json.dumps(
                {
                    "법령": {
                        "기본정보": {
                            "법령ID": "001556",
                            "법령명_한글": "관세법",
                            "시행일자": effective,
                            "공포일자": "20260801",
                            "제개정구분": "일부개정",
                            "소관부처": "관세청",
                        },
                        "조문": {
                            "조문단위": {
                                "조문여부": "조문",
                                "조문번호": "0001",
                                "조문가지번호": "00",
                                "조문제목": "목적",
                                "조문내용": text,
                            }
                        },
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    first, first_hash = write_body(
        "first.json", mst="100", effective="20260805", text="기준일 조문"
    )
    second, second_hash = write_body(
        "second.json", mst="101", effective="20260806", text="개정 조문"
    )

    def asset(path: Path, digest: str, *, mst: str, effective: str, kind: str) -> dict[str, object]:
        relative = str(path.relative_to(profile)).replace("\\", "/")
        return {
            "law_id": "001556",
            "current_mst": mst,
            "current_file": relative,
            "current_sha256": digest,
            "effective_mst": mst,
            "effective_file": relative,
            "effective_sha256": digest,
            "effective_date": f"{effective[:4]}-{effective[4:6]}-{effective[6:]}",
            "article_count": 1,
            "is_repealed": False,
            "law_name": "관세법",
            "hierarchy": "법률",
            "ministry": "관세청",
            "official_uri": f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={mst}",
            "collected_at": datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
            "source_kind": kind,
        }

    assets = (
        asset(first, first_hash, mst="100", effective="20260805", kind="baseline"),
        asset(second, second_hash, mst="101", effective="20260806", kind="refresh"),
    )
    expected = _source_article_projection_records(profile=profile, source_assets=assets)
    assert [(row["version_mst"], row["valid_to"]) for row in expected] == [
        ("100", "2026-08-06"),
        ("101", None),
    ]

    from src.ingest.laws import build_meta, parse_articles

    contents_by_mst: dict[str, str] = {}
    for path, mst in ((first, "100"), (second, "101")):
        body = json.loads(path.read_text(encoding="utf-8"))
        rows = parse_articles(body, build_meta(body, mst))
        contents_by_mst[mst] = str(rows[0]["content"])

    database_rows = [
        (
            row["law_id"],
            row["article_no"],
            row["paragraph_no"],
            None,
            row["title"],
            contents_by_mst[str(row["version_mst"])],
            row["content_hash"],
            row["version_mst"],
            date.fromisoformat(str(row["valid_from"])),
            date.fromisoformat(str(row["valid_to"])) if row["valid_to"] else None,
            row["parent_law_id"],
            row["parent_article_no"],
            row["parent_paragraph_no"],
            row["parent_version_mst"],
        )
        for row in expected
    ]

    class Cursor:
        def execute(self, _query: str) -> None:
            return None

        def fetchall(self) -> list[tuple[object, ...]]:
            return database_rows

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    from src.db import postgres

    monkeypatch.setattr(postgres, "connect", lambda: Connection())
    assert len(_verify_source_article_projection(profile=profile, source_assets=assets)) == 64

    # A title-only mutation is a user-visible citation error and must be
    # rejected even if an attacker leaves the old content hash in place.
    database_rows[1] = (*database_rows[1][:4], "다른 제목", *database_rows[1][5:])
    with pytest.raises(LawHistoryError, match="SOURCE_ARTICLE_PROJECTION_MISMATCH"):
        _verify_source_article_projection(profile=profile, source_assets=assets)


def test_temporal_qdrant_projection_rehashes_db_text_and_checks_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vector payload cannot keep an old hash, text, or citation identity."""

    from src.db import postgres, qdrant
    from src.ingest.laws import temporal_point_id

    content = "[관세법 제1조(목적)]\n검증용 본문"
    content_hash = _hash(content)
    database_row = (
        7,
        "001556",
        "관세법",
        "법률",
        100,
        None,
        None,
        content,
        "100",
        content_hash,
        date(2026, 8, 5),
        None,
    )

    class Cursor:
        def execute(self, _query: str) -> None:
            return None

        def fetchall(self) -> list[tuple[object, ...]]:
            return [database_row]

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    payload = {
        "article_pk": 7,
        "law_id": "001556",
        "law_name": "관세법",
        "hierarchy": "법률",
        "article_no": 100,
        "paragraph_no": None,
        "item_no": None,
        "version_mst": "100",
        "content_hash": content_hash,
        "valid_from_ordinal": date(2026, 8, 5).toordinal(),
        "valid_to_ordinal": date.max.toordinal(),
        "text": content,
    }

    class QdrantClient:
        def collection_exists(self, _collection: str) -> bool:
            return True

        def count(self, _collection: str, *, exact: bool) -> SimpleNamespace:
            assert exact is True
            return SimpleNamespace(count=1)

        def scroll(self, **_kwargs: object) -> tuple[list[SimpleNamespace], None]:
            return [SimpleNamespace(id=temporal_point_id(7, "100"), payload=payload)], None

    monkeypatch.setattr(postgres, "connect", lambda: Connection())
    monkeypatch.setattr(qdrant, "client", lambda: QdrantClient())
    assert len(_verify_temporal_store_projection()) == 64

    payload["text"] = "다른 본문"
    with pytest.raises(LawHistoryError, match="TEMPORAL_STORE_MISMATCH"):
        _verify_temporal_store_projection()


def test_law_history_store_provenance_requires_exact_manifest_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collected_at = datetime(2026, 8, 9, tzinfo=timezone.utc)
    asset = {
        "law_id": "001556",
        "effective_mst": "100",
        "official_uri": "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=100",
        "effective_sha256": "a" * 64,
        "collected_at": collected_at.isoformat(),
        "effective_date": "2026-08-05",
        "article_count": 3,
        "is_repealed": False,
        "law_name": "관세법",
        "hierarchy": "법률",
        "ministry": "관세청",
        "source_kind": "baseline",
    }

    class Cursor:
        def execute(self, _query: str) -> None:
            return None

        def fetchall(self) -> list[tuple[object, ...]]:
            return [
                (
                    "001556",
                    "100",
                    "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=100",
                    "a" * 64,
                    collected_at,
                    date(2026, 8, 5),
                    3,
                    False,
                    "관세법",
                    "법률",
                    "관세청",
                    "baseline",
                    date(2026, 8, 5),
                )
            ]

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    class Connection:
        def cursor(self) -> Cursor:
            return Cursor()

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    from src.db import postgres

    monkeypatch.setattr(postgres, "connect", lambda: Connection())
    _validate_store_source_provenance((asset,))

    asset["effective_sha256"] = "b" * 64
    with pytest.raises(LawHistoryError, match="SOURCE_PROVENANCE_MISMATCH"):
        _validate_store_source_provenance((asset,))


def test_law_history_explicit_reset_only_removes_active_marker(tmp_path) -> None:
    from src.law_history import discard_active_law_history_manifest
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    source = profile / "data" / "law-history" / "previous" / "law.json"
    source.parent.mkdir(parents=True)
    source.write_text("official-local-source", encoding="utf-8")
    marker = profile / "state" / "active-law-history-manifest.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")

    assert discard_active_law_history_manifest(local_root_override=tmp_path) is True
    assert not marker.exists()
    assert source.read_text(encoding="utf-8") == "official-local-source"
    assert discard_active_law_history_manifest(local_root_override=tmp_path) is False


def test_law_history_advances_article_intervals_individually(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future article provision must not close unchanged text early."""

    from src.ingest import laws

    cursor = _HistoryCursor(
        open_rows=[
            (1, 100, None, _hash("unchanged"), date(2026, 8, 5), None),
            (2, 200, None, _hash("old"), date(2026, 8, 5), None),
            (3, 300, None, _hash("removed"), date(2026, 8, 5), None),
        ]
    )
    inserted: list[tuple[int, date]] = []
    monkeypatch.setattr(laws, "upsert_law_history", lambda _cur, _meta: None)
    monkeypatch.setattr(
        laws,
        "_insert_temporal_article_row",
        lambda _cur, *, row, **_kwargs: (
            inserted.append((row["article_no"], row["valid_from"])) or 1000 + len(inserted)
        ),
    )
    rows = [
        {
            "article_no": 100,
            "paragraph_no": None,
            "title": None,
            "content": "unchanged",
            "valid_from": date(2026, 8, 5),
            "children": [],
        },
        {
            "article_no": 200,
            "paragraph_no": None,
            "title": None,
            "content": "new future provision",
            "valid_from": date(2026, 8, 10),
            "children": [],
        },
    ]

    result = laws.load_law_history_version(
        cursor,
        {
            "law_id": "001556",
            "mst": "mst-2",
            "enforcement_date": "20260808",
            "law_name": "관세법",
            "ministry": "관세청",
            "promulgation_date": "20260801",
            "revision_type": "일부개정",
        },
        rows,
    )

    assert result == {"inserted": 1, "already_loaded": False}
    assert inserted == [(200, date(2026, 8, 10))]
    assert [params for _query, params in cursor.updates] == [
        (date(2026, 8, 10), 2),
        (date(2026, 8, 8), 3),
    ]


def test_law_history_rejects_missing_predecessor_after_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.ingest import laws

    cursor = _HistoryCursor(open_rows=[])
    monkeypatch.setattr(laws, "upsert_law_history", lambda _cur, _meta: None)
    with pytest.raises(ValueError, match="omits a prior effective article"):
        laws.load_law_history_version(
            cursor,
            {
                "law_id": "001556",
                "mst": "mst-2",
                "enforcement_date": "20260808",
                "law_name": "관세법",
                "ministry": "관세청",
                "promulgation_date": "20260801",
                "revision_type": "일부개정",
            },
            [
                {
                    "article_no": 100,
                    "paragraph_no": None,
                    "title": None,
                    "content": "old provision without baseline predecessor",
                    "valid_from": date(2026, 8, 5),
                    "children": [],
                }
            ],
        )


def test_law_history_repeal_closes_every_open_article_without_new_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.ingest import laws

    cursor = _HistoryCursor(
        open_rows=[
            (1, 100, None, _hash("before-repeal"), date(2026, 8, 5), None),
            (2, 200, None, _hash("another-open-article"), date(2026, 8, 5), None),
        ]
    )
    monkeypatch.setattr(laws, "upsert_law_history", lambda _cur, _meta: None)

    result = laws.load_law_history_version(
        cursor,
        {
            "law_id": "001556",
            "mst": "mst-3",
            "enforcement_date": "20260808",
            "promulgation_date": "20260801",
            "law_name": "관세법",
            "ministry": "관세청",
            "revision_type": "폐지",
            "is_repealed": True,
        },
        [],
    )

    assert result == {"inserted": 0, "already_loaded": False}
    assert [params for _query, params in cursor.updates] == [
        (date(2026, 8, 8), 1),
        (date(2026, 8, 8), 2),
    ]


def test_exact_effective_repeal_body_is_collected_but_empty_nonrepeal_is_rejected(tmp_path) -> None:
    from src.law_history import (
        LawHistoryVersionCandidate,
        _collect_catalog_candidate,
    )

    def repeal_body(revision_type: str) -> dict:
        return {
            "법령": {
                "기본정보": {
                    "법령ID": "001556",
                    "법령명_한글": "관세법",
                    "시행일자": "20260808",
                    "공포일자": "20260801",
                    "제개정구분": revision_type,
                    "소관부처": "관세청",
                },
                "조문": {"조문단위": []},
            }
        }

    candidate = LawHistoryVersionCandidate("001556", "103", date(2026, 8, 8))
    source = _collect_catalog_candidate(
        source_root=tmp_path,
        candidate=candidate,
        source_kind="refresh",
        api_get=lambda _endpoint, **_params: repeal_body("폐지"),
    )

    assert source.is_repealed is True
    assert source.article_count == 0
    assert source.law_name == "관세법"

    with pytest.raises(LawHistoryError, match="EFFECTIVE_ARTICLES_MISSING"):
        _collect_catalog_candidate(
            source_root=tmp_path / "nonrepeal",
            candidate=candidate,
            source_kind="refresh",
            api_get=lambda _endpoint, **_params: repeal_body("일부개정"),
        )
