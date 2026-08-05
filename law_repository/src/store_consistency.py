"""Explicit, complete consistency checks for an initialized local Data store.

The normal Evidence readiness path deliberately checks only the small state it
needs to serve a review.  This module is the opt-in maintenance check: it
reads every relevant PostgreSQL, Qdrant, and Neo4j projection row and verifies
their shared identifiers and invariants.  It has no store-driver imports at
module import time, so a normal ``--group dev`` checkout stays lightweight.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

_COLLECTION = "customs_law_current"
_SCROLL_PAGE_SIZE = 256
ArticleIdentity = tuple[str, int, int | None, int | None]


class StoreVerificationError(RuntimeError):
    """A content-free classification for an explicit local-store check."""


@dataclass(frozen=True, slots=True)
class IndexedArticle:
    point_id: str
    article_pk: int
    law_id: str
    law_name: str
    hierarchy: str
    article_no: int
    paragraph_no: int | None
    item_no: int | None
    enforcement_date: str
    text: str

    def payload(self) -> dict[str, object]:
        return {
            "article_pk": self.article_pk,
            "law_id": self.law_id,
            "law_name": self.law_name,
            "hierarchy": self.hierarchy,
            "article_no": self.article_no,
            "paragraph_no": self.paragraph_no,
            "item_no": self.item_no,
            "enforcement_date": self.enforcement_date,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class PostgresStoreState:
    law_ids: frozenset[str]
    current_article_counts: dict[str, int]
    current_article_hashes: dict[ArticleIdentity, str]
    indexed_articles: tuple[IndexedArticle, ...]
    graph_articles: dict[int, str]
    snapshot_id: str
    snapshot_content_hash: str
    snapshot_query_count: int
    snapshot_raw_row_count: int


@dataclass(frozen=True, slots=True)
class GraphStoreState:
    article_law_ids: dict[int, str]
    hsk_codes: frozenset[str]
    requirement_snapshot_ids: frozenset[str]
    ccct_requirement_count: int
    self_loops: int
    cross_law_cites: int
    same_law_refers: int
    invalid_delegates: int
    non_leaf_hsk_labels: int
    invalid_requirement_sources: int
    orphaned_requirements: int


def _count(cursor: Any, query: str, params: object | None = None) -> int:
    cursor.execute(query, params)
    row = cursor.fetchone()
    if row is None:
        raise StoreVerificationError("STORE_VERIFICATION_QUERY_FAILED")
    return int(row[0])


def _validate_law_hash_rows(rows: list[Any]) -> None:
    """Validate all stored law content bytes and their version provenance."""

    if not rows:
        raise StoreVerificationError("STORE_LAW_CORPUS_EMPTY")
    for content, content_hash, version_mst, has_version in rows:
        if (
            not isinstance(content, str)
            or not content.strip()
            or not isinstance(content_hash, str)
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != content_hash
            or not isinstance(version_mst, str)
            or not version_mst.strip()
            or not has_version
        ):
            raise StoreVerificationError("STORE_LAW_CONTENT_HASH_MISMATCH")


def _read_postgres_state() -> PostgresStoreState:
    """Read all law-ledger facts needed for the cross-store comparison."""

    from src.db.postgres import connect

    with connect() as conn, conn.cursor() as cursor:
        law_ids = frozenset(str(row[0]) for row in _rows(cursor, "SELECT law_id FROM laws"))
        if not law_ids:
            raise StoreVerificationError("STORE_LAW_CORPUS_EMPTY")

        if _count(
            cursor,
            """
            SELECT count(*) FROM law_articles a JOIN law_articles b
              ON a.id < b.id
             AND a.law_id = b.law_id AND a.article_no = b.article_no
             AND a.paragraph_no IS NOT DISTINCT FROM b.paragraph_no
             AND a.item_no IS NOT DISTINCT FROM b.item_no
             AND daterange(a.valid_from, a.valid_to, '[)')
                 && daterange(b.valid_from, b.valid_to, '[)')
            """,
        ):
            raise StoreVerificationError("STORE_LAW_INTERVAL_OVERLAP")

        if _count(
            cursor,
            """
            SELECT count(*) FROM law_articles
            WHERE is_current <> (
              valid_from <= current_date
              AND (valid_to > current_date OR valid_to IS NULL)
            )
            """,
        ):
            raise StoreVerificationError("STORE_LAW_CURRENT_FLAG_MISMATCH")

        if _count(
            cursor,
            """
            SELECT count(*) FROM law_articles a
            WHERE (a.qdrant_point_id IS NOT NULL) <> (
              a.is_current AND NOT EXISTS (
                SELECT 1 FROM law_articles child WHERE child.parent_article_pk = a.id
              )
            )
            """,
        ):
            raise StoreVerificationError("STORE_LAW_INDEXABILITY_MISMATCH")

        hash_rows = _rows(
            cursor,
            """
            SELECT a.content, a.content_hash, a.version_mst,
                   EXISTS (
                     SELECT 1 FROM law_versions v
                     WHERE v.law_id = a.law_id AND v.mst = a.version_mst
                   )
            FROM law_articles a
            """,
        )
        _validate_law_hash_rows(hash_rows)

        count_rows = _rows(
            cursor,
            """
            SELECT law_id, count(*) FROM law_articles
            WHERE is_current
            GROUP BY law_id
            """,
        )
        current_article_counts = {str(law_id): int(count) for law_id, count in count_rows}
        current_hash_rows = _rows(
            cursor,
            """
            SELECT law_id, article_no, paragraph_no, item_no, content_hash
            FROM law_articles
            WHERE is_current
            """,
        )
        current_article_hashes = {
            (
                str(law_id),
                int(article_no),
                int(paragraph_no) if paragraph_no is not None else None,
                int(item_no) if item_no is not None else None,
            ): str(content_hash)
            for law_id, article_no, paragraph_no, item_no, content_hash in current_hash_rows
        }
        if len(current_article_hashes) != len(current_hash_rows):
            raise StoreVerificationError("STORE_LAW_CURRENT_IDENTITY_DUPLICATE")

        indexed_rows = _rows(
            cursor,
            """
            SELECT a.qdrant_point_id::text, a.id, a.law_id, l.law_name,
                   l.hierarchy, a.article_no, a.paragraph_no, a.item_no,
                   a.valid_from::text, a.content
            FROM law_articles a JOIN laws l USING (law_id)
            WHERE a.is_current
              AND NOT EXISTS (
                SELECT 1 FROM law_articles child WHERE child.parent_article_pk = a.id
              )
            ORDER BY a.id
            """,
        )
        indexed_articles: list[IndexedArticle] = []
        for row in indexed_rows:
            point_id = row[0]
            if not isinstance(point_id, str) or not point_id:
                raise StoreVerificationError("STORE_LAW_INDEXABILITY_MISMATCH")
            indexed_articles.append(
                IndexedArticle(
                    point_id=point_id,
                    article_pk=int(row[1]),
                    law_id=str(row[2]),
                    law_name=str(row[3]),
                    hierarchy=str(row[4]),
                    article_no=int(row[5]),
                    paragraph_no=int(row[6]) if row[6] is not None else None,
                    item_no=int(row[7]) if row[7] is not None else None,
                    enforcement_date=str(row[8]),
                    text=str(row[9]),
                )
            )
        if not indexed_articles:
            raise StoreVerificationError("STORE_LAW_INDEX_EMPTY")

        graph_rows = _rows(
            cursor,
            """
            SELECT id, law_id FROM law_articles
            WHERE is_current AND paragraph_no IS NULL
            """,
        )
        graph_articles = {int(article_pk): str(law_id) for article_pk, law_id in graph_rows}
        if len(graph_articles) != len(graph_rows) or not graph_articles:
            raise StoreVerificationError("STORE_GRAPH_SOURCE_INVALID")

        snapshots = _rows(
            cursor,
            """
            SELECT snapshot_id, content_hash, query_count, raw_row_count
            FROM knowledge_snapshots
            WHERE source_type = 'ccct' AND is_current
            """,
        )
        if len(snapshots) != 1:
            raise StoreVerificationError("STORE_CCCT_CURRENT_SNAPSHOT_INVALID")
        snapshot_id, content_hash, query_count, raw_row_count = snapshots[0]
        if (
            not isinstance(snapshot_id, str)
            or not snapshot_id
            or not isinstance(content_hash, str)
            or not content_hash
        ):
            raise StoreVerificationError("STORE_CCCT_CURRENT_SNAPSHOT_INVALID")

    return PostgresStoreState(
        law_ids=law_ids,
        current_article_counts=current_article_counts,
        current_article_hashes=current_article_hashes,
        indexed_articles=tuple(indexed_articles),
        graph_articles=graph_articles,
        snapshot_id=snapshot_id,
        snapshot_content_hash=content_hash,
        snapshot_query_count=int(query_count),
        snapshot_raw_row_count=int(raw_row_count),
    )


def _rows(cursor: Any, query: str, params: object | None = None) -> list[Any]:
    cursor.execute(query, params)
    return list(cursor.fetchall())


def _scroll_qdrant_points(client: Any, *, expected_count: int) -> tuple[Any, ...]:
    """Read every point once with finite pagination and no vectors."""

    if expected_count < 0:
        raise StoreVerificationError("STORE_QDRANT_COUNT_INVALID")
    rows: list[Any] = []
    offset: object | None = None
    seen_offsets: set[str] = set()
    for _page in range(expected_count + 1):
        points, next_offset = client.scroll(
            collection_name=_COLLECTION,
            limit=_SCROLL_PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if len(points) > _SCROLL_PAGE_SIZE:
            raise StoreVerificationError("STORE_QDRANT_PAGE_INVALID")
        rows.extend(points)
        if len(rows) > expected_count:
            raise StoreVerificationError("STORE_QDRANT_COUNT_MISMATCH")
        if next_offset is None:
            return tuple(rows)
        marker = repr(next_offset)
        if marker in seen_offsets:
            raise StoreVerificationError("STORE_QDRANT_PAGINATION_INVALID")
        seen_offsets.add(marker)
        offset = next_offset
    raise StoreVerificationError("STORE_QDRANT_PAGINATION_INVALID")


def _read_qdrant_points() -> tuple[Any, ...]:
    from src.db.qdrant import client as qdrant_client

    client = qdrant_client()
    if not client.collection_exists(_COLLECTION):
        raise StoreVerificationError("STORE_QDRANT_COLLECTION_MISSING")
    count = int(client.count(_COLLECTION, exact=True).count)
    return _scroll_qdrant_points(client, expected_count=count)


def _validate_qdrant_projection(
    expected_articles: tuple[IndexedArticle, ...], qdrant_points: tuple[Any, ...]
) -> None:
    expected_by_id = {article.point_id: article for article in expected_articles}
    if len(expected_by_id) != len(expected_articles):
        raise StoreVerificationError("STORE_POSTGRES_DUPLICATE_POINT_ID")

    actual_by_id: dict[str, Any] = {}
    payload_article_pks: set[int] = set()
    for point in qdrant_points:
        point_id = str(point.id)
        if point_id in actual_by_id:
            raise StoreVerificationError("STORE_QDRANT_DUPLICATE_POINT_ID")
        payload = point.payload
        if not isinstance(payload, Mapping):
            raise StoreVerificationError("STORE_QDRANT_PAYLOAD_MISMATCH")
        article_pk = payload.get("article_pk")
        if not isinstance(article_pk, int) or article_pk in payload_article_pks:
            raise StoreVerificationError("STORE_QDRANT_ARTICLE_MAPPING_MISMATCH")
        payload_article_pks.add(article_pk)
        actual_by_id[point_id] = payload

    if set(actual_by_id) != set(expected_by_id):
        raise StoreVerificationError("STORE_QDRANT_ID_MISMATCH")
    if payload_article_pks != {article.article_pk for article in expected_articles}:
        raise StoreVerificationError("STORE_QDRANT_ARTICLE_MAPPING_MISMATCH")
    for point_id, expected in expected_by_id.items():
        if dict(actual_by_id[point_id]) != expected.payload():
            raise StoreVerificationError("STORE_QDRANT_PAYLOAD_MISMATCH")


def _graph_count(session: Any, query: str) -> int:
    result = session.run(query).single()
    if result is None:
        raise StoreVerificationError("STORE_GRAPH_QUERY_FAILED")
    return int(result["count"])


def _read_graph_state() -> GraphStoreState:
    from src.db.neo4j import driver as make_driver

    driver = make_driver()
    try:
        with driver.session() as session:
            article_rows = list(
                session.run(
                    "MATCH (a:Article) RETURN a.article_pk AS article_pk, a.law_id AS law_id"
                )
            )
            article_law_ids: dict[int, str] = {}
            for row in article_rows:
                article_pk = row["article_pk"]
                law_id = row["law_id"]
                if (
                    not isinstance(article_pk, int)
                    or not isinstance(law_id, str)
                    or article_pk in article_law_ids
                ):
                    raise StoreVerificationError("STORE_GRAPH_ARTICLE_MAPPING_INVALID")
                article_law_ids[article_pk] = law_id

            hsk_codes = frozenset(
                str(row["code"]) for row in session.run("MATCH (n:HSNode) RETURN n.code AS code")
            )
            requirement_rows = list(
                session.run(
                    "MATCH (r:Requirement {source_type: 'ccct'}) "
                    "RETURN DISTINCT r.snapshot_id AS snapshot_id"
                )
            )
            requirement_snapshot_ids = frozenset(
                str(row["snapshot_id"])
                for row in requirement_rows
                if isinstance(row["snapshot_id"], str) and row["snapshot_id"]
            )
            ccct_requirement_count = _graph_count(
                session,
                "MATCH (r:Requirement {source_type: 'ccct'}) RETURN count(r) AS count",
            )
            self_loops = _graph_count(
                session,
                "MATCH (a:Article)-[r:CITES|DELEGATES|REFERS_TO]->(a) RETURN count(r) AS count",
            )
            cross_law_cites = _graph_count(
                session,
                "MATCH (s:Article)-[:CITES]->(t:Article) "
                "WHERE s.law_id <> t.law_id RETURN count(*) AS count",
            )
            same_law_refers = _graph_count(
                session,
                "MATCH (s:Article)-[:REFERS_TO]->(t:Article) "
                "WHERE s.law_id = t.law_id RETURN count(*) AS count",
            )
            invalid_delegates = _graph_count(
                session,
                """
                MATCH (source:Article)-[:DELEGATES]->(target:Article)
                WITH CASE source.hierarchy
                       WHEN '법률' THEN 1 WHEN '시행령' THEN 2 WHEN '시행규칙' THEN 3 ELSE 99
                     END AS source_rank,
                     CASE target.hierarchy
                       WHEN '법률' THEN 1 WHEN '시행령' THEN 2 WHEN '시행규칙' THEN 3 ELSE 99
                     END AS target_rank
                WHERE source_rank >= target_rank
                RETURN count(*) AS count
                """,
            )
            non_leaf_hsk_labels = _graph_count(
                session,
                """
                MATCH (n:HSNode)
                WHERE (n:HSK) <> (n.level = 10)
                RETURN count(n) AS count
                """,
            )
            invalid_requirement_sources = _graph_count(
                session,
                """
                MATCH (source)-[:HAS_REQUIREMENT]->(:Requirement)
                WHERE NOT source:HSK OR source.level <> 10
                RETURN count(*) AS count
                """,
            )
            orphaned_requirements = _graph_count(
                session,
                """
                MATCH (requirement:Requirement {source_type: 'ccct'})
                WHERE NOT (requirement)-[:BASED_ON]->(:Law)
                   OR NOT (requirement)-[:APPROVED_BY]->(:Agency)
                   OR NOT (requirement)-[:REQUIRES_DOCUMENT]->(:Certificate)
                RETURN count(requirement) AS count
                """,
            )
    finally:
        driver.close()
    return GraphStoreState(
        article_law_ids=article_law_ids,
        hsk_codes=hsk_codes,
        requirement_snapshot_ids=requirement_snapshot_ids,
        ccct_requirement_count=ccct_requirement_count,
        self_loops=self_loops,
        cross_law_cites=cross_law_cites,
        same_law_refers=same_law_refers,
        invalid_delegates=invalid_delegates,
        non_leaf_hsk_labels=non_leaf_hsk_labels,
        invalid_requirement_sources=invalid_requirement_sources,
        orphaned_requirements=orphaned_requirements,
    )


def _validate_graph_projection(expected_articles: dict[int, str], graph: GraphStoreState) -> None:
    if graph.article_law_ids != expected_articles:
        raise StoreVerificationError("STORE_GRAPH_ARTICLE_MAPPING_MISMATCH")
    if graph.self_loops:
        raise StoreVerificationError("STORE_GRAPH_SELF_LOOP")
    if graph.cross_law_cites:
        raise StoreVerificationError("STORE_GRAPH_CITES_SCOPE_INVALID")
    if graph.same_law_refers:
        raise StoreVerificationError("STORE_GRAPH_REFERS_SCOPE_INVALID")
    if graph.invalid_delegates:
        raise StoreVerificationError("STORE_GRAPH_DELEGATE_DIRECTION_INVALID")
    if graph.non_leaf_hsk_labels:
        raise StoreVerificationError("STORE_GRAPH_HSK_LABEL_INVALID")
    if graph.invalid_requirement_sources:
        raise StoreVerificationError("STORE_GRAPH_REQUIREMENT_SOURCE_INVALID")
    if graph.orphaned_requirements:
        raise StoreVerificationError("STORE_GRAPH_REQUIREMENT_ORPHANED")


def _validate_source_inputs(postgres: PostgresStoreState, graph: GraphStoreState) -> None:
    """Compare retained tracked source inputs without printing their contents."""

    from src.ingest.ccct import build_dataset
    from src.ingest.hsk import load_level_names

    ccct = build_dataset()
    if (
        ccct.snapshot_id != postgres.snapshot_id
        or ccct.content_hash != postgres.snapshot_content_hash
        or len(ccct.queries) != postgres.snapshot_query_count
        or len(ccct.source_records) != postgres.snapshot_raw_row_count
    ):
        raise StoreVerificationError("STORE_CCCT_SOURCE_MISMATCH")
    if ccct.requirements and graph.requirement_snapshot_ids != frozenset({ccct.snapshot_id}):
        raise StoreVerificationError("STORE_GRAPH_CCCT_SNAPSHOT_MISMATCH")
    if not ccct.requirements and graph.ccct_requirement_count:
        raise StoreVerificationError("STORE_GRAPH_CCCT_SNAPSHOT_MISMATCH")

    source_hsk_codes = frozenset(str(code) for code in load_level_names()["code"])
    if not source_hsk_codes or graph.hsk_codes != source_hsk_codes:
        raise StoreVerificationError("STORE_GRAPH_HSK_SOURCE_MISMATCH")


def _validate_expected_laws(
    postgres: PostgresStoreState,
    *,
    expected_law_ids: Collection[str] | None,
    expected_current_article_counts: Mapping[str, int] | None,
    expected_current_article_hashes: Mapping[ArticleIdentity, str] | None,
) -> None:
    if expected_law_ids is not None and postgres.law_ids != frozenset(expected_law_ids):
        raise StoreVerificationError("STORE_EXPECTED_LAW_SET_MISMATCH")
    if expected_current_article_counts is not None:
        for law_id, expected_count in expected_current_article_counts.items():
            if (
                not isinstance(expected_count, int)
                or expected_count < 1
                or postgres.current_article_counts.get(law_id) != expected_count
            ):
                raise StoreVerificationError("STORE_EXPECTED_LAW_ARTICLE_COUNT_MISMATCH")
    if expected_current_article_hashes is not None and postgres.current_article_hashes != dict(
        expected_current_article_hashes
    ):
        raise StoreVerificationError("STORE_EXPECTED_LAW_CONTENT_MISMATCH")


def verify_complete_store_consistency(
    *,
    expected_law_ids: Collection[str] | None = None,
    expected_current_article_counts: Mapping[str, int] | None = None,
    expected_current_article_hashes: Mapping[ArticleIdentity, str] | None = None,
    verify_source_inputs: bool = True,
) -> dict[str, int]:
    """Validate every local Data projection only when explicitly requested.

    The optional ``expected_*`` inputs let the bounded three-law UAT bind its
    already-validated raw source state to PostgreSQL.  A normal ``dev-store
    verify`` leaves them unset, so it remains generic for the configured
    corpus.
    """

    try:
        postgres = _read_postgres_state()
        _validate_expected_laws(
            postgres,
            expected_law_ids=expected_law_ids,
            expected_current_article_counts=expected_current_article_counts,
            expected_current_article_hashes=expected_current_article_hashes,
        )
        qdrant_points = _read_qdrant_points()
        _validate_qdrant_projection(postgres.indexed_articles, qdrant_points)
        graph = _read_graph_state()
        _validate_graph_projection(postgres.graph_articles, graph)
        if verify_source_inputs:
            _validate_source_inputs(postgres, graph)
    except StoreVerificationError:
        raise
    except Exception:
        # Driver errors may contain DSNs, local paths, or response details.
        # The command surfaces only this stable classification.
        raise StoreVerificationError("STORE_VERIFICATION_UNAVAILABLE") from None
    return {
        "laws": len(postgres.law_ids),
        "indexed_articles": len(postgres.indexed_articles),
        "graph_articles": len(postgres.graph_articles),
        "qdrant_points": len(qdrant_points),
        "hsk_nodes": len(graph.hsk_codes),
        "ccct_requirements": graph.ccct_requirement_count,
    }


__all__ = [
    "GraphStoreState",
    "ArticleIdentity",
    "IndexedArticle",
    "PostgresStoreState",
    "StoreVerificationError",
    "verify_complete_store_consistency",
]
