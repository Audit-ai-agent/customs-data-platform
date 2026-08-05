"""Pure tests for the explicit full local-store consistency coordinator."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.store_consistency as consistency


def _article() -> consistency.IndexedArticle:
    return consistency.IndexedArticle(
        point_id="point-1",
        article_pk=1,
        law_id="001556",
        law_name="관세법",
        hierarchy="법률",
        article_no=100,
        paragraph_no=None,
        item_no=None,
        enforcement_date="2026-08-05",
        text="법령 본문",
    )


def _postgres() -> consistency.PostgresStoreState:
    article = _article()
    return consistency.PostgresStoreState(
        law_ids=frozenset({article.law_id}),
        current_article_counts={article.law_id: 1},
        current_article_hashes={(article.law_id, article.article_no, None, None): "b" * 64},
        indexed_articles=(article,),
        graph_articles={article.article_pk: article.law_id},
        snapshot_id="ccct_snapshot",
        snapshot_content_hash="a" * 64,
        snapshot_query_count=1,
        snapshot_raw_row_count=1,
    )


def _graph(**overrides: object) -> consistency.GraphStoreState:
    values: dict[str, object] = {
        "article_law_ids": {1: "001556"},
        "hsk_codes": frozenset({"0101210000"}),
        "requirement_snapshot_ids": frozenset({"ccct_snapshot"}),
        "ccct_requirement_count": 1,
        "self_loops": 0,
        "cross_law_cites": 0,
        "same_law_refers": 0,
        "invalid_delegates": 0,
        "non_leaf_hsk_labels": 0,
        "invalid_requirement_sources": 0,
        "orphaned_requirements": 0,
    }
    values.update(overrides)
    return consistency.GraphStoreState(**values)  # type: ignore[arg-type]


def test_complete_check_coordinates_all_three_stores_without_source_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    article = _article()
    calls: list[str] = []
    monkeypatch.setattr(
        consistency, "_read_postgres_state", lambda: calls.append("pg") or _postgres()
    )
    monkeypatch.setattr(
        consistency,
        "_read_qdrant_points",
        lambda: (
            calls.append("qdrant")
            or (SimpleNamespace(id=article.point_id, payload=article.payload()),)
        ),
    )
    monkeypatch.setattr(consistency, "_read_graph_state", lambda: calls.append("neo4j") or _graph())

    result = consistency.verify_complete_store_consistency(verify_source_inputs=False)

    assert calls == ["pg", "qdrant", "neo4j"]
    assert result == {
        "laws": 1,
        "indexed_articles": 1,
        "graph_articles": 1,
        "qdrant_points": 1,
        "hsk_nodes": 1,
        "ccct_requirements": 1,
    }


def test_qdrant_payload_mapping_is_checked_for_every_point() -> None:
    article = _article()
    bad_payload = article.payload() | {"law_name": "different"}

    with pytest.raises(consistency.StoreVerificationError, match="STORE_QDRANT_PAYLOAD_MISMATCH"):
        consistency._validate_qdrant_projection(
            (article,),
            (SimpleNamespace(id=article.point_id, payload=bad_payload),),
        )


def test_qdrant_equal_count_id_replacement_and_duplicates_are_rejected() -> None:
    article = _article()
    with pytest.raises(consistency.StoreVerificationError, match="STORE_QDRANT_ID_MISMATCH"):
        consistency._validate_qdrant_projection(
            (article,),
            (SimpleNamespace(id="replacement-point", payload=article.payload()),),
        )
    with pytest.raises(consistency.StoreVerificationError, match="STORE_QDRANT_DUPLICATE_POINT_ID"):
        consistency._validate_qdrant_projection(
            (article,),
            (
                SimpleNamespace(id=article.point_id, payload=article.payload()),
                SimpleNamespace(id=article.point_id, payload=article.payload()),
            ),
        )


def test_graph_edge_invariants_are_classified_without_graph_values() -> None:
    with pytest.raises(
        consistency.StoreVerificationError,
        match="STORE_GRAPH_DELEGATE_DIRECTION_INVALID",
    ):
        consistency._validate_graph_projection({1: "001556"}, _graph(invalid_delegates=1))

    with pytest.raises(
        consistency.StoreVerificationError, match="STORE_GRAPH_ARTICLE_MAPPING_MISMATCH"
    ):
        consistency._validate_graph_projection({1: "001556"}, _graph(article_law_ids={2: "001556"}))


@pytest.mark.parametrize(
    ("content", "content_hash", "version_mst", "has_version"),
    [
        ("", hashlib.sha256(b"").hexdigest(), "mst-1", True),
        ("tampered", "0" * 64, "mst-1", True),
        ("article", hashlib.sha256(b"article").hexdigest(), " ", True),
        ("article", hashlib.sha256(b"article").hexdigest(), "missing", False),
    ],
)
def test_law_content_hash_and_version_invariants_are_checked(
    content: str,
    content_hash: str,
    version_mst: str,
    has_version: bool,
) -> None:
    with pytest.raises(consistency.StoreVerificationError, match="STORE_LAW_CONTENT_HASH_MISMATCH"):
        consistency._validate_law_hash_rows([(content, content_hash, version_mst, has_version)])


def test_qdrant_scroll_reads_all_pages_with_bounded_payloads() -> None:
    calls: list[dict[str, object]] = []

    class FakeQdrant:
        def scroll(self, **kwargs: object):
            calls.append(kwargs)
            if kwargs["offset"] is None:
                return [SimpleNamespace(id="point-1")], "next-page"
            return [SimpleNamespace(id="point-2")], None

    points = consistency._scroll_qdrant_points(FakeQdrant(), expected_count=2)
    assert [str(point.id) for point in points] == ["point-1", "point-2"]
    assert [call["offset"] for call in calls] == [None, "next-page"]
    assert all(call["limit"] == 256 for call in calls)
    assert all(call["with_payload"] is True for call in calls)
    assert all(call["with_vectors"] is False for call in calls)


def test_expected_law_set_and_source_counts_are_opt_in_bounds() -> None:
    postgres = _postgres()
    consistency._validate_expected_laws(
        postgres,
        expected_law_ids=("001556",),
        expected_current_article_counts={"001556": 1},
        expected_current_article_hashes={("001556", 100, None, None): "b" * 64},
    )
    with pytest.raises(
        consistency.StoreVerificationError, match="STORE_EXPECTED_LAW_ARTICLE_COUNT_MISMATCH"
    ):
        consistency._validate_expected_laws(
            postgres,
            expected_law_ids=("001556",),
            expected_current_article_counts={"001556": 2},
            expected_current_article_hashes=None,
        )
    with pytest.raises(
        consistency.StoreVerificationError, match="STORE_EXPECTED_LAW_CONTENT_MISMATCH"
    ):
        consistency._validate_expected_laws(
            postgres,
            expected_law_ids=("001556",),
            expected_current_article_counts={"001556": 1},
            expected_current_article_hashes={("001556", 100, None, None): "c" * 64},
        )


def test_store_consistency_import_keeps_default_setup_free_of_store_clients() -> None:
    root = Path(__file__).resolve().parents[2]
    program = "\n".join(
        (
            "from pathlib import Path",
            "import sys",
            "root = Path.cwd()",
            "sys.path[:0] = [str(root), str(root / 'law_repository')]",
            "import src.store_consistency",
            "blocked = {'psycopg', 'neo4j', 'qdrant_client', 'pandas', 'pydantic_settings'}",
            "loaded = sorted(name for name in blocked if any(module == name or module.startswith(name + '.') for module in sys.modules))",
            "raise SystemExit(','.join(loaded) if loaded else 0)",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
