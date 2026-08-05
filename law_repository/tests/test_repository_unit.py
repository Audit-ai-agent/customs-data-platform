"""Pure repository policy tests; real-store checks remain in test_repository.py."""

from __future__ import annotations

from datetime import date

import pytest

from src import repository as repo


def test_temporal_predicate_current_uses_today() -> None:
    sql, params = repo._temporal_predicate(None)
    assert params["as_of"] == date.today()
    assert "valid_from <= %(as_of)s" in sql
    assert "valid_to > %(as_of)s OR valid_to IS NULL" in sql


def test_temporal_predicate_as_of_passthrough() -> None:
    value = date(2024, 3, 1)
    _, params = repo._temporal_predicate(value)
    assert params["as_of"] == value


def test_normalize_hsk_accepts_only_hs6_or_hsk10_without_padding() -> None:
    assert repo.normalize_hsk("0101 21 0000") == "0101210000"
    assert repo.normalize_hsk("854231") == "854231"
    with pytest.raises(ValueError):
        repo.normalize_hsk("8542")
    with pytest.raises(ValueError):
        repo.normalize_hsk("123456789012345")
    assert repo.normalize_hsk("0204100000")[0] == "0"


def test_resolve_hsk_candidates_exact_unique_and_ambiguous() -> None:
    leaves = ["7318151000", "7318159000", "0303540000"]
    assert repo.resolve_hsk_candidates("0303540000", leaves).status == "exact"
    unique = repo.resolve_hsk_candidates("030354", leaves)
    assert unique.status == "unique_descendant"
    assert unique.resolved_hsk10 == "0303540000"
    ambiguous = repo.resolve_hsk_candidates("731815", leaves)
    assert ambiguous.status == "ambiguous"
    assert ambiguous.resolved_hsk10 is None
    assert list(ambiguous.candidate_hsk10s) == ["7318151000", "7318159000"]


def test_search_unknown_arm_raises() -> None:
    with pytest.raises(ValueError):
        repo.search("아무거나", arm="rerank")
