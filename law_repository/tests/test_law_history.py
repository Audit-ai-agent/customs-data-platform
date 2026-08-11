"""Pure law-history profile boundaries; no Law API or local stores required."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.functional_uat import canonical_json_bytes
from src.hsk_ccct_history import (
    ActiveArchiveCatalog,
    ArchiveAsset,
    ArchiveManifest,
    CcctArchiveReceipt,
)
from src.law_history import (
    LAW_HISTORY_BASELINE_AS_OF,
    LAW_HISTORY_PROFILE,
    FunctionalLawHistoryEvidenceBackend,
    LawHistoryError,
    LawHistorySource,
    LawHistoryVersionCandidate,
    _CollectedCatalogCandidate,
    _candidate_as_of,
    _candidate_group_as_of,
    _canonicalize_coeffective_candidates,
    _combined_refresh_assets,
    _finish_pending_refresh,
    _catalog_for_law,
    _journal_refresh_sources,
    _neo4j_property_projection,
    _is_valid_baseline_asset,
    _is_rebindable_stale_frozen_inputs,
    _profile_source_file,
    _refresh_record,
    _recovery_baseline_entries,
    _source_article_projection_records,
    _updated_refresh_manifest,
    _validated_refresh_records,
    _validate_refresh_receipt_assets,
    _validate_refresh_ledger,
    _validate_ready_refresh_manifest_binding,
    collect_law_history_baseline,
    collect_law_history_refresh,
    recover_law_history_baseline,
    rebind_law_history_frozen_inputs,
    _validate_source_asset,
    _verify_temporal_store_projection,
    _verify_source_article_projection,
    _validate_store_source_provenance,
    _write_active_manifest_if_absent,
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


def _coeffective_receipt(
    *, mst: str, projection: str, raw_tag: str, effective_date: date = date(2026, 8, 5)
) -> _CollectedCatalogCandidate:
    """Build a body-free official-receipt fixture for canonicalizer tests."""

    candidate = LawHistoryVersionCandidate("001556", mst, effective_date)
    source = LawHistorySource(
        law_id=candidate.law_id,
        current_mst=mst,
        current_file=f"{mst}.json",
        current_sha256=_hash(f"current-{raw_tag}"),
        effective_mst=mst,
        effective_file=f"{mst}.json",
        effective_sha256=_hash(f"effective-{raw_tag}"),
        effective_date=effective_date,
        article_count=1,
        is_repealed=False,
        law_name="관세법",
        hierarchy="법률",
        ministry="관세청",
        official_uri=f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={mst}",
        collected_at=datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
        source_kind="baseline",
    )
    return _CollectedCatalogCandidate(
        candidate=candidate,
        source=source,
        body={},
        projection_sha256=_hash(projection),
    )


def _old_and_new_response(*, predecessor: str, successor: str, law_id: str = "001556") -> dict:
    return {
        "OldAndNewService": {
            "구조문_기본정보": {"법령ID": law_id, "법령일련번호": predecessor},
            "신조문_기본정보": {"법령ID": law_id, "법령일련번호": successor},
        }
    }


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


def test_recovery_baseline_source_batch_rejects_path_escape_and_incomplete_manifest(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    (profile / "data" / "law-history" / "baseline-invalid").mkdir(parents=True)
    (profile / "data" / "law-history" / "baseline-invalid" / "source-manifest.json").write_text(
        json.dumps(
            {
                "local_state_version": "functional-law-history/v3",
                "profile": LAW_HISTORY_PROFILE,
                "as_of": "2026-08-05",
                "sources": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LawHistoryError, match="RECOVERY_SOURCE_BATCH_INVALID"):
        _recovery_baseline_entries(profile=profile, source_batch="../outside")
    with pytest.raises(LawHistoryError, match="RECOVERY_SOURCE_BATCH_INVALID"):
        _recovery_baseline_entries(profile=profile, source_batch="baseline-invalid")


def test_absent_manifest_recovery_publisher_never_overwrites_existing_marker(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    (profile / "state").mkdir(parents=True)
    first = {"marker": "first"}
    digest = _write_active_manifest_if_absent(profile, first)
    active = profile / "state" / "active-law-history-manifest.json"
    before = active.read_bytes()

    with pytest.raises(LawHistoryError, match="MANIFEST_CONCURRENT_MODIFICATION"):
        _write_active_manifest_if_absent(profile, {"marker": "second"})

    assert digest == hashlib.sha256(before).hexdigest()
    assert active.read_bytes() == before


def test_recover_baseline_runs_all_gates_before_absent_only_publication(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.law_history as history

    profile = tmp_path / "profile"
    source_root = profile / "data" / "law-history" / "baseline-recovery"
    (profile / "state").mkdir(parents=True)
    source_root.mkdir(parents=True)
    source = LawHistorySource(
        law_id="001556",
        current_mst="100",
        current_file="law.json",
        current_sha256=_hash("raw"),
        effective_mst="100",
        effective_file="law.json",
        effective_sha256=_hash("raw"),
        effective_date=LAW_HISTORY_BASELINE_AS_OF,
        article_count=1,
        is_repealed=False,
        law_name="관세법",
        hierarchy="법률",
        ministry="관세청",
        official_uri="https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=100",
        collected_at=datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
        source_kind="baseline",
    )
    artifact = tmp_path / "artifact.json"
    calls: list[str] = []
    published_manifest = {"published": "verified"}
    published_digest = hashlib.sha256(canonical_json_bytes(published_manifest)).hexdigest()

    monkeypatch.setattr(
        history, "ensure_functional_uat_law_history_profile", lambda **_kwargs: profile
    )
    monkeypatch.setattr(
        history, "apply_functional_uat_law_history_runtime_environment", lambda **_kwargs: profile
    )
    monkeypatch.setattr(
        history,
        "_recoverable_baseline_sources",
        lambda **_kwargs: (source_root, (source,)),
    )
    monkeypatch.setattr(
        history,
        "build_frozen_input_attestation",
        lambda: SimpleNamespace(manifest={"attested": "exact"}),
    )
    monkeypatch.setattr(
        history,
        "_verify_frozen_input_projections_full",
        lambda _attestation: calls.append("frozen"),
    )
    monkeypatch.setattr(
        history,
        "_law_history_model_inventory",
        lambda **_kwargs: {"dense": {"revision": "fixed"}},
    )
    monkeypatch.setattr(
        history,
        "_history_store_counts",
        lambda: {"law_count": 65, "law_version_count": 65},
    )
    monkeypatch.setattr(
        history,
        "_verify_source_article_projection",
        lambda **_kwargs: calls.append("source") or _hash("source"),
    )
    monkeypatch.setattr(
        history,
        "_verify_temporal_store_projection",
        lambda: calls.append("temporal") or _hash("temporal"),
    )
    monkeypatch.setattr(history, "_manifest_from_sources", lambda **_kwargs: published_manifest)
    monkeypatch.setattr(
        history,
        "validate_law_history_state",
        lambda **_kwargs: (published_manifest, published_digest),
    )
    monkeypatch.setattr(
        history,
        "verify_law_history_store",
        lambda **_kwargs: {"manifest_sha256": published_digest},
    )
    monkeypatch.setattr(history, "_new_public_artifact", lambda **_kwargs: artifact)

    assert (
        recover_law_history_baseline(
            source_batch="baseline-recovery",
            confirm_profile=LAW_HISTORY_PROFILE,
        )
        == artifact
    )
    assert calls == ["frozen", "source", "temporal"]
    assert json.loads((profile / "state" / "active-law-history-manifest.json").read_text()) == (
        published_manifest
    )


def _frozen_input_receipt(*, digest: str, byte_size: int) -> dict[str, object]:
    return {
        "attestation_kind": "functional-law-history/frozen-inputs/v1",
        "hsk_snapshot_as_of": "2026-08-05",
        "requirements_snapshot_as_of": "2026-08-05",
        "hsk": {
            "files": [],
            "node_count": 1,
            "leaf_count": 1,
            "projection_sha256": "b" * 64,
        },
        "ccct": {
            "relative_path": "fixed.jsonl",
            "input_sha256": digest,
            "byte_size": byte_size,
            "snapshot_id": "ccct_" + "c" * 64,
            "content_hash": "c" * 64,
            "query_count": 1,
            "raw_row_count": 1,
            "requirement_count": 1,
            "document_count": 1,
            "projection_sha256": "d" * 64,
        },
    }


def test_frozen_input_rebind_allows_only_a_stale_ccct_byte_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.law_history as history

    current = _frozen_input_receipt(digest="a" * 64, byte_size=10)
    stale = _frozen_input_receipt(digest="e" * 64, byte_size=12)
    monkeypatch.setattr(history, "_validate_frozen_inputs_manifest", lambda value: current)

    assert _is_rebindable_stale_frozen_inputs(stale=stale, current=current) is True

    changed_hsk = deepcopy(stale)
    changed_hsk["hsk"]["leaf_count"] = 2  # type: ignore[index]
    assert _is_rebindable_stale_frozen_inputs(stale=changed_hsk, current=current) is False

    changed_semantics = deepcopy(stale)
    changed_semantics["ccct"]["query_count"] = 2  # type: ignore[index]
    assert _is_rebindable_stale_frozen_inputs(stale=changed_semantics, current=current) is False

    malformed_receipt = deepcopy(stale)
    malformed_receipt["ccct"]["input_sha256"] = "not-a-digest"  # type: ignore[index]
    assert _is_rebindable_stale_frozen_inputs(stale=malformed_receipt, current=current) is False


def test_rebind_frozen_inputs_preserves_refreshes_and_never_collects_or_downloads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.law_history as history

    profile = tmp_path / "law-history-profile"
    (profile / "state").mkdir(parents=True)
    stale = _frozen_input_receipt(digest="e" * 64, byte_size=12)
    current = _frozen_input_receipt(digest="a" * 64, byte_size=10)
    source_assets = [{"asset": "retained"}]
    refreshes = [{"refresh": "retained"}]
    manifest: dict[str, object] = {
        "source_assets": source_assets,
        "refreshes": refreshes,
        "frozen_inputs": stale,
        "source_article_projection_sha256": "1" * 64,
        "temporal_store_projection_sha256": "2" * 64,
    }
    events: list[str] = []
    published_digest = "f" * 64
    replacement: dict[str, object] = {}

    monkeypatch.setattr(
        history, "ensure_functional_uat_law_history_profile", lambda **_kwargs: profile
    )
    monkeypatch.setattr(
        history,
        "apply_functional_uat_law_history_runtime_environment",
        lambda **_kwargs: profile,
    )
    monkeypatch.setattr(
        history,
        "build_frozen_input_attestation",
        lambda: SimpleNamespace(manifest=current),
    )
    monkeypatch.setattr(
        history,
        "_load_rebindable_frozen_input_manifest",
        lambda **_kwargs: (manifest, "0" * 64),
    )
    monkeypatch.setattr(
        history,
        "_validate_loaded_law_history_state",
        lambda **kwargs: (
            events.append("local-state")
            if kwargs["frozen_inputs"] == current
            else pytest.fail("stale fixed inputs were accepted")
        ),
    )
    monkeypatch.setattr(
        history,
        "_verify_frozen_input_projections_full",
        lambda attestation: (
            events.append("frozen-projection")
            if attestation.manifest == current
            else pytest.fail("unexpected fixed inputs")
        ),
    )
    monkeypatch.setattr(
        history,
        "_verify_source_article_projection",
        lambda **_kwargs: events.append("source-projection") or "1" * 64,
    )
    monkeypatch.setattr(
        history,
        "_verify_temporal_store_projection",
        lambda: events.append("temporal-projection") or "2" * 64,
    )

    def replace(**kwargs: object) -> str:
        updated = kwargs["manifest"]
        assert isinstance(updated, dict)
        replacement.update(updated)
        assert updated["frozen_inputs"] == current
        assert updated["source_assets"] is source_assets
        assert updated["refreshes"] is refreshes
        events.append("replace")
        return published_digest

    monkeypatch.setattr(history, "_replace_active_manifest_bytes_if_digest", replace)
    monkeypatch.setattr(
        history,
        "validate_law_history_state",
        lambda **_kwargs: (replacement, published_digest),
    )
    monkeypatch.setattr(
        history,
        "verify_law_history_store",
        lambda **_kwargs: {"manifest_sha256": published_digest},
    )
    monkeypatch.setattr(
        history,
        "validate_functional_uat_law_history_api_credential",
        lambda **_kwargs: pytest.fail("rebind must not read a collection credential"),
    )
    monkeypatch.setattr(
        history,
        "functional_uat_law_history_collection_environment",
        lambda **_kwargs: pytest.fail("rebind must not enter a collection environment"),
    )
    monkeypatch.setattr(
        history,
        "prefetch_models",
        lambda **_kwargs: pytest.fail("rebind must not download a model"),
    )
    monkeypatch.setattr(
        history,
        "collect_law_history_baseline",
        lambda **_kwargs: pytest.fail("rebind must not collect official sources"),
    )

    assert rebind_law_history_frozen_inputs(confirm_profile=LAW_HISTORY_PROFILE) == published_digest
    assert events == [
        "local-state",
        "frozen-projection",
        "source-projection",
        "temporal-projection",
        "replace",
    ]


def test_rebind_frozen_inputs_refuses_any_pending_refresh_before_reading_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.law_history as history

    profile = tmp_path / "law-history-profile"
    pending = profile / "state" / "pending-law-history-refresh.json"
    pending.parent.mkdir(parents=True)
    pending.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        history, "ensure_functional_uat_law_history_profile", lambda **_kwargs: profile
    )
    monkeypatch.setattr(
        history,
        "apply_functional_uat_law_history_runtime_environment",
        lambda **_kwargs: profile,
    )
    monkeypatch.setattr(
        history,
        "build_frozen_input_attestation",
        lambda: pytest.fail("pending refresh must stop rebind before fixed-input reads"),
    )

    with pytest.raises(LawHistoryError, match="LAW_HISTORY_REFRESH_JOURNAL_CONFLICT"):
        rebind_law_history_frozen_inputs(confirm_profile=LAW_HISTORY_PROFILE)


def test_hsk_projection_matches_neo4j_null_omission_without_masking_real_changes() -> None:
    """The full-store audit accepts only Neo4j's unavoidable null omission."""

    parsed_hsk_node = {
        "code": "0101210000",
        "level": 10,
        "name_ko": "말",
        "is_leaf": True,
        "quantity_unit": None,
        "weight_unit": "KG",
    }
    persisted_hsk_node = {
        "code": "0101210000",
        "level": 10,
        "name_ko": "말",
        "is_leaf": True,
        "weight_unit": "KG",
    }

    expected = canonical_json_bytes(_neo4j_property_projection(parsed_hsk_node))
    assert expected == canonical_json_bytes(persisted_hsk_node)
    assert expected != canonical_json_bytes({**persisted_hsk_node, "quantity_unit": "EA"})
    assert expected != canonical_json_bytes({**persisted_hsk_node, "weight_unit": "TON"})
    assert expected != canonical_json_bytes(
        {key: value for key, value in persisted_hsk_node.items() if key != "weight_unit"}
    )


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


def test_candidate_group_is_lazy_and_does_not_fetch_unselected_historical_conflict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the latest applicable date group may invoke body/edge retrieval."""

    import src.law_history as history

    monkeypatch.setattr(history, "LAW_HISTORY_LAW_IDS", ("001556",))
    calls: list[dict[str, object]] = []

    def body(mst: str, effective: str) -> dict[str, object]:
        return {
            "법령": {
                "기본정보": {
                    "법령ID": "001556",
                    "법령명_한글": "관세법",
                    "시행일자": effective,
                    "공포일자": "20200101",
                    "제개정구분": "일부개정",
                    "소관부처": "관세청",
                },
                "조문": {
                    "조문단위": {
                        "조문여부": "조문",
                        "조문번호": "0001",
                        "조문가지번호": "00",
                        "조문제목": "목적",
                        "조문내용": f"safe {mst}",
                    }
                },
            }
        }

    def api_get(endpoint: str, **params: object) -> dict[str, object]:
        calls.append({"endpoint": endpoint, **params})
        if endpoint == "lawSearch.do":
            if params["nw"] == "1":
                return {
                    "LawSearch": {
                        "totalCnt": "3",
                        "law": [
                            {
                                "법령ID": "001556",
                                "법령일련번호": "100",
                                "시행일자": "20200101",
                            },
                            {
                                "법령ID": "001556",
                                "법령일련번호": "101",
                                "시행일자": "20200101",
                            },
                            {
                                "법령ID": "001556",
                                "법령일련번호": "102",
                                "시행일자": "20260805",
                            },
                        ],
                    }
                }
            return {"LawSearch": {"totalCnt": "0", "law": []}}
        assert endpoint == "lawService.do"
        assert params["target"] == "eflaw"
        assert params["MST"] == "102"
        return body("102", "20260805")

    _root, sources = collect_law_history_baseline(
        profile=tmp_path, as_of=date(2026, 8, 5), api_get=api_get
    )
    assert [source.effective_mst for source in sources] == ["102"]
    assert all(
        not (call["endpoint"] == "lawService.do" and call["target"] == "oldAndNew")
        for call in calls
    )


def test_same_day_identical_projection_uses_deterministic_receipt_without_old_and_new() -> None:
    calls: list[dict[str, object]] = []

    def api_get(endpoint: str, **params: object) -> dict:
        calls.append({"endpoint": endpoint, **params})
        raise AssertionError("identical legal projections must not call oldAndNew")

    selected = _canonicalize_coeffective_candidates(
        receipts=(
            _coeffective_receipt(mst="101", projection="equal", raw_tag="b"),
            _coeffective_receipt(mst="100", projection="equal", raw_tag="a"),
        ),
        api_get=api_get,
    )
    assert selected.candidate.mst == "100"
    assert calls == []


def test_same_day_divergent_projection_requires_verified_official_unique_sink() -> None:
    calls: list[dict[str, object]] = []
    edges = {"100": "099", "101": "100"}

    def api_get(endpoint: str, **params: object) -> dict:
        assert endpoint == "lawService.do"
        assert params["target"] == "oldAndNew"
        mst = str(params["MST"])
        calls.append({"endpoint": endpoint, **params})
        return _old_and_new_response(predecessor=edges[mst], successor=mst)

    selected = _canonicalize_coeffective_candidates(
        receipts=(
            _coeffective_receipt(mst="100", projection="before", raw_tag="a"),
            _coeffective_receipt(mst="101", projection="after", raw_tag="b"),
        ),
        api_get=api_get,
    )
    assert selected.candidate.mst == "101"
    assert {str(call["MST"]) for call in calls} == {"100", "101"}


@pytest.mark.parametrize(
    ("edges", "law_id"),
    [
        ({"100": "099", "101": "098"}, "001556"),
        ({"100": "101", "101": "100"}, "001556"),
        ({"100": "099", "101": "100"}, "999999"),
    ],
)
def test_same_day_divergent_projection_fails_closed_without_a_single_verified_sink(
    edges: dict[str, str], law_id: str
) -> None:
    def api_get(_endpoint: str, **params: object) -> dict:
        mst = str(params["MST"])
        return _old_and_new_response(predecessor=edges[mst], successor=mst, law_id=law_id)

    with pytest.raises(LawHistoryError, match="CATALOG_CANONICAL_UNRESOLVED"):
        _canonicalize_coeffective_candidates(
            receipts=(
                _coeffective_receipt(mst="100", projection="a", raw_tag="a"),
                _coeffective_receipt(mst="101", projection="b", raw_tag="b"),
            ),
            api_get=api_get,
        )


def test_candidate_group_returns_all_same_day_candidates_before_canonicalization() -> None:
    candidates = (
        LawHistoryVersionCandidate("001556", "100", date(2026, 8, 5)),
        LawHistoryVersionCandidate("001556", "101", date(2026, 8, 5)),
        LawHistoryVersionCandidate("001556", "102", date(2026, 8, 6)),
    )
    group = _candidate_group_as_of(candidates, as_of=date(2026, 8, 5))
    assert [candidate.mst for candidate in group] == ["100", "101"]
    with pytest.raises(LawHistoryError, match="CATALOG_CANONICAL_REQUIRED"):
        _candidate_as_of(candidates, as_of=date(2026, 8, 5))


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


def test_phased_mst_refresh_keeps_one_receipt_per_effective_date(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One MST may have multiple phases; its raw receipts must not overwrite."""

    import src.law_history as history

    monkeypatch.setattr(history, "LAW_HISTORY_LAW_IDS", ("001556",))

    def body(effective: str) -> dict[str, object]:
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
                        "조문내용": f"phase {effective}",
                    }
                },
            }
        }

    def api_get(endpoint: str, **params: object) -> dict[str, object]:
        if endpoint == "lawSearch.do":
            if params["nw"] == "1":
                return {
                    "LawSearch": {
                        "totalCnt": "2",
                        "law": [
                            {
                                "법령ID": "001556",
                                "법령일련번호": "100",
                                "시행일자": "20260806",
                            },
                            {
                                "법령ID": "001556",
                                "법령일련번호": "100",
                                "시행일자": "20260807",
                            },
                        ],
                    }
                }
            return {"LawSearch": {"totalCnt": "0", "law": []}}
        assert endpoint == "lawService.do"
        assert params["MST"] == "100"
        return body(str(params["efYd"]))

    root, sources = collect_law_history_refresh(
        profile=tmp_path,
        from_date=date(2026, 8, 6),
        to_date=date(2026, 8, 7),
        api_get=api_get,
    )

    assert [(source.effective_mst, source.version_key) for source in sources] == [
        ("100", "100@20260806"),
        ("100", "100@20260807"),
    ]
    assert len({source.effective_file for source in sources}) == 2
    assert all((root / source.effective_file).is_file() for source in sources)
    receipt = _refresh_record(from_date=date(2026, 8, 6), to_date=date(2026, 8, 7), sources=sources)
    assert [row["version_key"] for row in receipt["sources"]] == [
        "100@20260806",
        "100@20260807",
    ]
    assert receipt["as_of_sources"] == [receipt["sources"][-1]]


def test_refresh_receipt_refuses_unresolved_same_day_mst_ambiguity() -> None:
    """Version keys do not authorize an arbitrary same-day canonical choice."""

    def source(mst: str) -> LawHistorySource:
        return LawHistorySource(
            law_id="001556",
            current_mst=mst,
            current_file=f"{mst}.json",
            current_sha256="a" * 64,
            effective_mst=mst,
            effective_file=f"{mst}.json",
            effective_sha256="a" * 64,
            effective_date=date(2026, 8, 6),
            article_count=1,
            is_repealed=False,
            law_name="관세법",
            hierarchy="법률",
            ministry="관세청",
            official_uri=f"https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq={mst}",
            collected_at=datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
            source_kind="refresh",
        )

    with pytest.raises(LawHistoryError, match="REFRESH_SAME_DAY_AMBIGUITY"):
        _refresh_record(
            from_date=date(2026, 8, 6),
            to_date=date(2026, 8, 6),
            sources=(source("100"), source("101")),
        )


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


def _archive_catalog_at(archive_as_of: date) -> ActiveArchiveCatalog:
    asset = ArchiveAsset(
        relative_path="hsk/levels.xlsx",
        official_uri="https://www.data.go.kr/data/15049722/fileData.do",
        sha256="a" * 64,
        byte_size=1,
        published_at=archive_as_of,
        effective_at=archive_as_of,
    )
    leaf_asset = ArchiveAsset(
        relative_path="hsk/leaf-attributes.xlsx",
        official_uri="https://www.data.go.kr/data/15130660/fileData.do",
        sha256="b" * 64,
        byte_size=1,
        published_at=archive_as_of,
        effective_at=archive_as_of,
    )
    receipt_asset = ArchiveAsset(
        relative_path="ccct/query-receipts.jsonl",
        official_uri="https://apis.data.go.kr/1220000/retrieveCcctLworCd/getRetrieveCcctLworCd",
        sha256="c" * 64,
        byte_size=1,
        published_at=archive_as_of,
        effective_at=archive_as_of,
    )
    manifest = ArchiveManifest(
        archive_id="hsk_ccct_" + "d" * 64,
        archive_as_of=archive_as_of,
        collected_at=datetime(2026, 8, 8, tzinfo=timezone.utc),
        manifest_sha256="e" * 64,
        directory=Path("archive-fixture"),
        level_names=asset,
        leaf_attributes=leaf_asset,
        ccct=CcctArchiveReceipt(
            official_uri=receipt_asset.official_uri,
            query_receipts=receipt_asset,
            snapshot_id="ccct_" + "f" * 64,
            query_count=1,
            empty_query_count=0,
            raw_row_count=1,
        ),
    )
    return ActiveArchiveCatalog(pointer_sha256="0" * 64, archives=(manifest,))


def test_temporal_backend_requires_an_exact_active_hsk_ccct_archive_after_baseline() -> None:
    requested = date(2026, 8, 8)
    backend = FunctionalLawHistoryEvidenceBackend(
        coverage_from=LAW_HISTORY_BASELINE_AS_OF,
        coverage_through=date(2026, 8, 10),
        archive_catalog=ActiveArchiveCatalog(pointer_sha256="a" * 64, archives=()),
    )

    support = backend.temporal_evidence_support("snapshot", requested)

    assert support.context.hsk_snapshot_as_of == LAW_HISTORY_BASELINE_AS_OF
    assert support.context.requirements_snapshot_as_of == LAW_HISTORY_BASELINE_AS_OF
    assert support.hsk_coverage_available is False
    assert support.requirements_coverage_available is False
    assert support.temporal_coverage is not None
    assert support.temporal_coverage.model_dump() == {
        "law": "covered",
        "hsk": "unavailable",
        "requirements": "unavailable",
    }


def test_temporal_backend_binds_exact_active_hsk_ccct_archive_to_requested_date() -> None:
    requested = date(2026, 8, 8)
    catalog = _archive_catalog_at(requested)
    backend = FunctionalLawHistoryEvidenceBackend(
        coverage_from=LAW_HISTORY_BASELINE_AS_OF,
        coverage_through=date(2026, 8, 10),
        archive_catalog=catalog,
    )

    support = backend.temporal_evidence_support("snapshot", requested)
    archive = catalog.for_date(requested)
    assert archive is not None
    assert support.context.hsk_snapshot_as_of == requested
    assert support.context.requirements_snapshot_as_of == requested
    assert support.hsk_coverage_available is True
    assert support.requirements_coverage_available is True
    assert support.hsk_snapshot_id == archive.hsk_snapshot_id
    assert support.requirements_snapshot_id == archive.ccct.snapshot_id


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


def test_refresh_variant_receipt_requires_equal_legal_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A changed raw receipt may prove coverage only when legal content agrees."""

    import src.law_history as history

    canonical_sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 6), digest="a" * 64, mst_offset=10_000
    )
    refreshed_sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 6), digest="b" * 64, mst_offset=10_000
    )
    canonical_assets = [
        source.local_state(profile=tmp_path, source_root=tmp_path) for source in canonical_sources
    ]
    receipt_assets = [
        source.local_state(profile=tmp_path, source_root=tmp_path) for source in refreshed_sources
    ]
    refresh = _refresh_record(
        from_date=date(2026, 8, 6), to_date=date(2026, 8, 6), sources=refreshed_sources
    )
    refresh_with_receipts = {**refresh, "receipt_assets": receipt_assets}

    assert _validated_refresh_records(
        refreshes=[refresh_with_receipts],
        source_assets=canonical_assets,
        coverage_through=date(2026, 8, 6),
    ) == (refresh_with_receipts,)
    with pytest.raises(LawHistoryError, match="REFRESH_CHAIN_INVALID"):
        _validated_refresh_records(
            refreshes=[refresh],
            source_assets=canonical_assets,
            coverage_through=date(2026, 8, 6),
        )
    monkeypatch.setattr(history, "_validate_source_asset", lambda *_args: None)
    monkeypatch.setattr(history, "_source_receipt_content_sha256", lambda *_args: "c" * 64)
    _validate_refresh_receipt_assets(
        profile=tmp_path,
        refreshes=[refresh_with_receipts],
        source_assets=canonical_assets,
    )
    combined, new_assets = _combined_refresh_assets(
        manifest={"source_assets": canonical_assets},
        staged_assets=receipt_assets,
        profile=tmp_path,
    )
    assert combined == canonical_assets
    assert new_assets == ()

    def divergent_projection(_profile: Path, asset: object) -> str:
        raw = asset if isinstance(asset, dict) else {}
        return "c" * 64 if raw.get("effective_sha256") == "a" * 64 else "d" * 64

    monkeypatch.setattr(history, "_source_receipt_content_sha256", divergent_projection)
    with pytest.raises(LawHistoryError, match="REFRESH_SOURCE_CONFLICT"):
        _validate_refresh_receipt_assets(
            profile=tmp_path,
            refreshes=[refresh_with_receipts],
            source_assets=canonical_assets,
        )
    with pytest.raises(LawHistoryError, match="REFRESH_SOURCE_CONFLICT"):
        _combined_refresh_assets(
            manifest={"source_assets": canonical_assets},
            staged_assets=receipt_assets,
            profile=tmp_path,
        )


def test_ready_refresh_manifest_is_bound_to_the_prior_manifest_and_receipts(tmp_path: Path) -> None:
    """A durable ready journal cannot substitute sources or its final receipt row."""

    sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 6), digest="a" * 64, mst_offset=10_000
    )
    assets = [source.local_state(profile=tmp_path, source_root=tmp_path) for source in sources]
    refresh = _refresh_record(from_date=date(2026, 8, 6), to_date=date(2026, 8, 6), sources=sources)
    previous = {"source_assets": assets, "refreshes": []}
    ready = {
        "source_assets": assets,
        "refreshes": [{**refresh, "receipt_assets": assets}],
    }

    _validate_ready_refresh_manifest_binding(
        profile=tmp_path,
        previous_manifest=previous,
        staged_assets=assets,
        refresh=refresh,
        ready_manifest=ready,
    )

    substituted_sources = dict(ready)
    substituted_sources["source_assets"] = [*assets[:-1], {**assets[-1], "law_name": "다른 법령"}]
    with pytest.raises(LawHistoryError, match="REFRESH_JOURNAL_CONFLICT"):
        _validate_ready_refresh_manifest_binding(
            profile=tmp_path,
            previous_manifest=previous,
            staged_assets=assets,
            refresh=refresh,
            ready_manifest=substituted_sources,
        )

    substituted_receipts = dict(ready)
    substituted_receipts["refreshes"] = [{**refresh, "receipt_assets": list(reversed(assets))}]
    with pytest.raises(LawHistoryError, match="REFRESH_JOURNAL_CONFLICT"):
        _validate_ready_refresh_manifest_binding(
            profile=tmp_path,
            previous_manifest=previous,
            staged_assets=assets,
            refresh=refresh,
            ready_manifest=substituted_receipts,
        )


def test_refresh_ledger_allows_only_the_prepublication_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ready-journal preflight can precede one exact ledger insertion only."""

    from src.db import postgres

    refresh = {
        "refresh_id": "a" * 64,
        "from": "2026-08-06",
        "to": "2026-08-06",
        "source_sha256": "b" * 64,
    }
    rows: list[tuple[object, ...]] = []

    class Cursor:
        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> None:
            return None

        def fetchall(self) -> list[tuple[object, ...]]:
            return rows

    class Connection:
        def __enter__(self) -> "Connection":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    monkeypatch.setattr(postgres, "connect", lambda: Connection())

    _validate_refresh_ledger([refresh], allow_unrecorded_final=True)
    with pytest.raises(LawHistoryError, match="REFRESH_LEDGER_MISMATCH"):
        _validate_refresh_ledger([refresh])

    rows[:] = [("a" * 64, date(2026, 8, 6), date(2026, 8, 6), "b" * 64)]
    _validate_refresh_ledger([refresh])
    rows.append(("c" * 64, date(2026, 8, 7), date(2026, 8, 7), "d" * 64))
    with pytest.raises(LawHistoryError, match="REFRESH_LEDGER_MISMATCH"):
        _validate_refresh_ledger([refresh], allow_unrecorded_final=True)


def test_ready_refresh_preflight_cannot_write_a_ledger_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed ready manifest must fail before the idempotent ledger write."""

    import src.law_history as history

    refresh = {"refresh_id": "a" * 64, "source_sha256": "b" * 64}
    journal = {
        "phase": "ready",
        "previous_manifest_sha256": "c" * 64,
        "refresh": refresh,
        "source_assets": [],
        "updated_manifest": {},
    }
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(history, "_load_pending_refresh", lambda _profile: journal)
    monkeypatch.setattr(history, "_journal_refresh_sources", lambda **_kwargs: ((), (), refresh))
    monkeypatch.setattr(history, "_load_active_manifest", lambda _profile: ({}, "c" * 64))
    monkeypatch.setattr(history, "_validate_ready_refresh_manifest_binding", lambda **_kwargs: None)

    def reject_preflight(**_kwargs: object) -> None:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_INVALID")

    monkeypatch.setattr(history, "_validate_publishable_refresh_manifest", reject_preflight)
    monkeypatch.setattr(history, "_record_refresh", lambda **kwargs: recorded.append(kwargs))

    with pytest.raises(LawHistoryError, match="REFRESH_JOURNAL_INVALID"):
        _finish_pending_refresh(
            profile=tmp_path,
            current_manifest_sha256="c" * 64,
            from_date=date(2026, 8, 6),
            to_date=date(2026, 8, 6),
        )
    assert recorded == []


def test_refresh_variant_receipt_allows_only_contact_department_metadata(tmp_path: Path) -> None:
    """Raw receipt variance is limited to one documented non-legal subtree."""

    def body(
        *,
        contact_name: str,
        promulgation_date: str = "20260801",
        revision_type: str = "일부개정",
        article_content: str = "검증 조문",
    ) -> dict[str, object]:
        return {
            "법령": {
                "기본정보": {
                    "법령ID": "001556",
                    "법령명_한글": "관세법",
                    "시행일자": "20260806",
                    "공포일자": promulgation_date,
                    "제개정구분": revision_type,
                    "소관부처": "관세청",
                    "연락부서": {"부서명": contact_name, "전화번호": "000"},
                },
                "조문": {
                    "조문단위": {
                        "조문여부": "조문",
                        "조문번호": "0001",
                        "조문가지번호": "00",
                        "조문제목": "목적",
                        "조문내용": article_content,
                    }
                },
            }
        }

    def asset(filename: str, value: dict[str, object]) -> dict[str, object]:
        path = tmp_path / filename
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        return LawHistorySource(
            law_id="001556",
            current_mst="100",
            current_file=filename,
            current_sha256=digest,
            effective_mst="100",
            effective_file=filename,
            effective_sha256=digest,
            effective_date=date(2026, 8, 6),
            article_count=1,
            is_repealed=False,
            law_name="관세법",
            hierarchy="법률",
            ministry="관세청",
            official_uri="https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=100",
            collected_at=datetime(2026, 8, 9, tzinfo=timezone.utc).isoformat(),
            source_kind="refresh",
        ).local_state(profile=tmp_path, source_root=tmp_path)

    canonical = asset("canonical.json", body(contact_name="담당부서-A"))
    contact_variant = asset("contact.json", body(contact_name="담당부서-B"))
    refreshes = [{"receipt_assets": [contact_variant]}]

    _validate_refresh_receipt_assets(
        profile=tmp_path,
        refreshes=refreshes,
        source_assets=[canonical],
    )
    combined, new_assets = _combined_refresh_assets(
        manifest={"source_assets": [canonical]},
        staged_assets=[contact_variant],
        profile=tmp_path,
    )
    assert combined == [canonical]
    assert new_assets == ()

    for index, variant in enumerate(
        (
            body(contact_name="담당부서-A", promulgation_date="20260802"),
            body(contact_name="담당부서-A", revision_type="전부개정"),
            body(contact_name="담당부서-A", article_content="변경 조문"),
        )
    ):
        rejected = asset(f"rejected-{index}.json", variant)
        with pytest.raises(LawHistoryError, match="REFRESH_SOURCE_CONFLICT"):
            _validate_refresh_receipt_assets(
                profile=tmp_path,
                refreshes=[{"receipt_assets": [rejected]}],
                source_assets=[canonical],
            )
        with pytest.raises(LawHistoryError, match="REFRESH_SOURCE_CONFLICT"):
            _combined_refresh_assets(
                manifest={"source_assets": [canonical]},
                staged_assets=[rejected],
                profile=tmp_path,
            )

    (tmp_path / "contact.json").write_text("{}", encoding="utf-8")
    with pytest.raises(LawHistoryError, match="SOURCE_HASH_MISMATCH"):
        _validate_refresh_receipt_assets(
            profile=tmp_path,
            refreshes=refreshes,
            source_assets=[canonical],
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


def test_refresh_invalidates_a_pre_refresh_benchmark_binding(tmp_path: Path) -> None:
    baseline_sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 5), digest="a" * 64, mst_offset=10_000
    )
    refresh_sources = _complete_refresh_sources(
        effective_date=date(2026, 8, 6), digest="b" * 64, mst_offset=20_000
    )
    baseline_assets = [
        source.local_state(profile=tmp_path, source_root=tmp_path) for source in baseline_sources
    ]
    staged_assets = [
        source.local_state(profile=tmp_path, source_root=tmp_path) for source in refresh_sources
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
    assert updated["refreshes"][0]["receipt_assets"] == staged_assets


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
            "version_key": source.version_key,
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


def test_temporal_index_orders_full_text_work_by_length_without_losing_row_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU padding optimization may change write order, never row identity."""

    from src.embed import dense
    from src.ingest import laws

    # tuple positions mirror index_temporal_qdrant's private SELECT exactly.
    rows = [
        (
            20,
            "law",
            "name",
            "법률",
            2,
            None,
            None,
            date(2026, 8, 5),
            None,
            "aa",
            "2",
            "2@20260805",
            "b" * 64,
        ),
        (
            10,
            "law",
            "name",
            "법률",
            1,
            None,
            None,
            date(2026, 8, 5),
            None,
            "bb",
            "1",
            "1@20260805",
            "a" * 64,
        ),
        (
            30,
            "law",
            "name",
            "법률",
            3,
            None,
            None,
            date(2026, 8, 5),
            None,
            "long",
            "3",
            "3@20260805",
            "c" * 64,
        ),
    ]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, _query: str) -> None:
            return None

        def fetchall(self):
            return rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def cursor(self):
            return Cursor()

    class Qdrant:
        def __init__(self) -> None:
            self.batches: list[list[object]] = []

        def upsert(self, *, collection_name: str, points: list[object]) -> None:
            assert collection_name == laws.TEMPORAL_COLLECTION
            self.batches.append(points)

    qdrant = Qdrant()
    encoded: list[tuple[list[str], int]] = []
    vectors_by_text = {"bb": [1.0], "aa": [2.0], "long": [3.0]}

    def fake_encode(texts: list[str], *, batch_size: int) -> np.ndarray:
        encoded.append((texts, batch_size))
        return np.asarray([vectors_by_text[text] for text in texts], dtype="float32")

    monkeypatch.setattr(laws, "connect", lambda: Connection())
    monkeypatch.setattr(laws, "qdrant_client", lambda: qdrant)
    monkeypatch.setattr(laws, "ensure_temporal_collection", lambda _client: None)
    monkeypatch.setattr(laws.bm25, "encode_doc", lambda _text: ([], []))
    monkeypatch.setattr(dense, "encode", fake_encode)

    assert laws.index_temporal_qdrant(batch_size=2) == 3
    assert encoded == [(["bb", "aa"], 8), (["long"], 8)]
    points = [point for batch in qdrant.batches for point in batch]
    observed = {
        point.payload["article_pk"]: {
            "id": point.id,
            "version_key": point.payload["version_key"],
            "content_hash": point.payload["content_hash"],
            "vector": point.vector[laws.DENSE_VECTOR],
        }
        for point in points
    }
    assert observed == {
        10: {
            "id": laws.temporal_point_id(10, "1@20260805"),
            "version_key": "1@20260805",
            "content_hash": "a" * 64,
            "vector": [1.0],
        },
        20: {
            "id": laws.temporal_point_id(20, "2@20260805"),
            "version_key": "2@20260805",
            "content_hash": "b" * 64,
            "vector": [2.0],
        },
        30: {
            "id": laws.temporal_point_id(30, "3@20260805"),
            "version_key": "3@20260805",
            "content_hash": "c" * 64,
            "vector": [3.0],
        },
    }


def test_law_history_source_path_rejects_windows_and_parent_escapes(tmp_path) -> None:
    profile = tmp_path / "profile"
    source = profile / "data" / "law-history" / "run" / "source.json"
    source.parent.mkdir(parents=True)
    source.write_text("{}", encoding="utf-8")

    assert _profile_source_file(profile, "data/law-history/run/source.json") == source
    for unsafe in ("../outside.json", r"\outside.json", r"C:\outside.json"):
        with pytest.raises(LawHistoryError, match="SOURCE_PATH_INVALID"):
            _profile_source_file(profile, unsafe)


class _WindowsProcessCheckError(OSError):
    """Portable fixture for the Windows ``os.kill(pid, 0)`` result."""

    def __init__(self, winerror: int) -> None:
        super().__init__(22, "process check failed")
        self.winerror = winerror


@pytest.mark.parametrize(
    ("error", "platform_name", "expected_live"),
    [
        (ProcessLookupError(), "posix", False),
        (_WindowsProcessCheckError(87), "nt", False),
        (_WindowsProcessCheckError(87), "posix", True),
        (PermissionError(), "nt", True),
        (_WindowsProcessCheckError(5), "nt", True),
        (OSError(), "nt", True),
    ],
)
def test_mutation_owner_liveness_only_recovers_known_missing_processes(
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    platform_name: str,
    expected_live: bool,
) -> None:
    """Windows invalid-parameter means a missing PID; all other errors retain locks."""

    from src import law_history as history

    def raise_process_error(_pid: int, _signal: int) -> None:
        raise error

    monkeypatch.setattr(history.os, "name", platform_name)
    monkeypatch.setattr(history.os, "kill", raise_process_error)

    assert history._mutation_owner_is_live(123456) is expected_live


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


def test_refresh_recovery_keeps_lock_for_an_unbound_ready_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Operator recovery cannot release a lock for a substituted ready state."""

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
        "phase": "ready",
        "previous_manifest_sha256": "a" * 64,
        "refresh": {},
        "source_assets": [{}],
        "updated_manifest": {},
    }
    monkeypatch.setattr(history, "_load_pending_refresh", lambda _profile: journal)
    monkeypatch.setattr(history, "_journal_refresh_sources", lambda **_kwargs: ((), (), {}))
    monkeypatch.setattr(history, "_load_active_manifest", lambda _profile: ({}, "a" * 64))
    monkeypatch.setattr(history, "_mutation_owner_is_live", lambda _pid: False)

    def reject_binding(**_kwargs: object) -> None:
        raise LawHistoryError("LAW_HISTORY_REFRESH_JOURNAL_CONFLICT")

    monkeypatch.setattr(history, "_validate_ready_refresh_manifest_binding", reject_binding)
    with pytest.raises(LawHistoryError, match="REFRESH_JOURNAL_CONFLICT"):
        history.recover_law_history_refresh_lock(
            from_date=date(2026, 8, 6),
            to_date=date(2026, 8, 6),
            confirm_profile=LAW_HISTORY_PROFILE,
            local_root_override=tmp_path,
        )
    assert lock.exists()


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


def test_interrupted_build_requires_recovery_before_markerless_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A reset never bypasses a dead build lock or removes raw local sources."""

    from src import law_history as history
    from src.local_profiles import ensure_functional_uat_law_history_profile

    profile = ensure_functional_uat_law_history_profile(root=tmp_path)
    source = profile / "data" / "laws" / "official-source.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("official-local-source", encoding="utf-8")
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

    with pytest.raises(LawHistoryError, match="MUTATION_LOCKED"):
        history.discard_active_law_history_manifest(local_root_override=tmp_path)
    assert lock.exists()
    assert source.read_text(encoding="utf-8") == "official-local-source"

    assert (
        history.recover_law_history_build_lock(
            confirm_profile=LAW_HISTORY_PROFILE,
            local_root_override=tmp_path,
        )
        is True
    )
    assert history.discard_active_law_history_manifest(local_root_override=tmp_path) is False
    assert not lock.exists()
    assert source.read_text(encoding="utf-8") == "official-local-source"


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
        "version_key": "100@20260805",
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
            "version_key": f"{mst}@{effective}",
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
            row["version_key"],
            date.fromisoformat(str(row["valid_from"])),
            date.fromisoformat(str(row["valid_to"])) if row["valid_to"] else None,
            row["parent_law_id"],
            row["parent_article_no"],
            row["parent_paragraph_no"],
            row["parent_version_mst"],
            row["parent_version_key"],
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
        "100@20260805",
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
        "version_key": "100@20260805",
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
            return [SimpleNamespace(id=temporal_point_id(7, "100@20260805"), payload=payload)], None

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
        "version_key": "100@20260805",
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
                    "100@20260805",
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
