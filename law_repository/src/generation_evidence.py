"""Evidence backend that can read only one immutable knowledge generation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import Any
from urllib.parse import quote

from psycopg.rows import dict_row
from qdrant_client import models

from src.db.neo4j import driver as make_driver
from src.db.postgres import connect
from src.db.qdrant import BM25_VECTOR, DENSE_VECTOR, client as make_qdrant
from src.embed import bm25
from src.evidence import LawArticleMatch, RequirementLookup
from src.generation import GenerationValidationError, SourceManifest, canonical_sha256
from src.generation_stores import qdrant_collection_name
from src.repository import HskResolution, normalize_hsk, resolve_hsk_candidates
from src.runtime_release import GenerationRequestLease


class GenerationEvidenceError(RuntimeError):
    """A generation-specific store invariant is unavailable or has drifted."""


def _default_dense_encoder(texts: list[str]) -> Any:
    from src.embed import dense

    return dense.encode(texts)


class GenerationEvidenceBackend:
    """Request-scoped backend pinned to a generation lease.

    No method resolves a legacy global Qdrant collection, Neo4j ``Article`` or
    unversioned active snapshot. PostgreSQL content comes from the immutable
    ``knowledge_generation_articles`` snapshot; CCCT is selected by the
    generation manifest; HSK and graph traversal require generation-tagged
    Neo4j projections.
    """

    def __init__(
        self,
        lease: GenerationRequestLease,
        *,
        connection_factory: Callable[[], Any] = connect,
        qdrant_factory: Callable[[], Any] = make_qdrant,
        neo4j_factory: Callable[[], Any] = make_driver,
        dense_encoder: Callable[[list[str]], Any] = _default_dense_encoder,
        sparse_encoder: Callable[[str], tuple[list[int], list[float]]] = (bm25.encode_query),
    ) -> None:
        self.lease = lease
        self.identity = lease.identity
        self._connection_factory = connection_factory
        self._qdrant_factory = qdrant_factory
        self._neo4j_factory = neo4j_factory
        self._dense_encoder = dense_encoder
        self._sparse_encoder = sparse_encoder
        self._manifest_cache: SourceManifest | None = None

    @property
    def generation_id(self) -> str:
        return self.identity.knowledge_generation_id

    def _manifest(self) -> SourceManifest:
        if self._manifest_cache is not None:
            return self._manifest_cache
        with self._connection_factory() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT source_manifest, source_manifest_sha256
                FROM knowledge_generations
                WHERE generation_id=%s AND state='active'
                """,
                (self.generation_id,),
            )
            row = cur.fetchone()
        if row is None:
            raise GenerationEvidenceError("generation source manifest is unavailable")
        try:
            manifest = SourceManifest.from_mapping(row[0])
            manifest.require_release_eligible_sources()
        except (TypeError, GenerationValidationError) as exc:
            raise GenerationEvidenceError("generation source manifest is invalid") from exc
        if (
            manifest.sha256 != row[1]
            or manifest.sha256 != self.identity.knowledge_source_manifest_sha256
        ):
            raise GenerationEvidenceError("generation source manifest has drifted")
        self._manifest_cache = manifest
        return manifest

    def _dataset(self, dataset_id: str):
        for item in self._manifest().datasets:
            if item.dataset_id == dataset_id:
                return item
        raise GenerationEvidenceError("generation dataset identity is unavailable")

    def _assert_postgres_pointer(self) -> None:
        with self._connection_factory() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT generation_id, epoch
                FROM knowledge_generation_active_pointer
                WHERE singleton
                """
            )
            pointer = cur.fetchone()
        if pointer != (self.generation_id, self.lease.generation_epoch):
            raise GenerationEvidenceError("PostgreSQL generation lease changed")

    def _store_attestation_counts(self) -> dict[str, int]:
        with self._connection_factory() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT store_name, article_count, model_manifest_sha256
                FROM knowledge_generation_store_attestations
                WHERE generation_id=%s
                ORDER BY store_name
                """,
                (self.generation_id,),
            )
            rows = cur.fetchall()
        result: dict[str, int] = {}
        for store, count, model_hash in rows:
            if model_hash != self.identity.model_manifest_sha256:
                raise GenerationEvidenceError("store model identity has drifted")
            result[str(store)] = int(count)
        if set(result) != {"postgresql", "qdrant", "neo4j"}:
            raise GenerationEvidenceError("generation store attestations are incomplete")
        if len(set(result.values())) != 1 or next(iter(result.values())) <= 0:
            raise GenerationEvidenceError("generation store article counts do not match")
        return result

    def _verify_postgres_laws(self, expected_articles: int) -> None:
        with self._connection_factory() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT count(*), count(DISTINCT law_id)
                FROM knowledge_generation_laws
                WHERE generation_id=%s
                """,
                (self.generation_id,),
            )
            law_count, distinct_laws = cur.fetchone()
            cur.execute(
                """
                SELECT count(*), count(DISTINCT article_pk),
                       count(DISTINCT law_id)
                FROM knowledge_generation_articles
                WHERE generation_id=%s
                  AND btrim(content) <> ''
                  AND content_sha256 ~ '^[0-9a-f]{64}$'
                """,
                (self.generation_id,),
            )
            article_count, distinct_articles, article_laws = cur.fetchone()
            cur.execute(
                """
                SELECT count(*)
                FROM knowledge_generation_laws l
                WHERE l.generation_id=%s
                  AND l.current_article_count <> (
                    SELECT count(*)
                    FROM knowledge_generation_articles a
                    WHERE a.generation_id=l.generation_id
                      AND a.law_id=l.law_id
                  )
                """,
                (self.generation_id,),
            )
            mismatched_laws = int(cur.fetchone()[0])
        if (
            int(law_count) != 58
            or int(distinct_laws) != 58
            or int(article_count) != expected_articles
            or int(distinct_articles) != expected_articles
            or int(article_laws) != 58
            or mismatched_laws
        ):
            raise GenerationEvidenceError("generation PostgreSQL law corpus is incomplete")

    def _verify_ccct(self) -> None:
        ccct = self._dataset("ccct")
        expected_queries = int(ccct.metrics.get("query_count", -1))
        expected_requirements = int(ccct.metrics.get("source_requirement_count", -1))
        expected_documents = int(ccct.metrics.get("document_count", -1))
        with self._connection_factory() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT source_definition, source_definition_sha256
                FROM knowledge_generation_datasets
                WHERE generation_id=%s AND dataset_id='ccct' AND snapshot_id=%s
                """,
                (self.generation_id, ccct.snapshot_id),
            )
            dataset_row = cur.fetchone()
            expected_definition = ccct.as_dict()
            if (
                dataset_row is None
                or dataset_row[0] != expected_definition
                or dataset_row[1] != canonical_sha256(expected_definition)
            ):
                raise GenerationEvidenceError("generation CCCT dataset identity has drifted")
            cur.execute(
                """
                SELECT count(*)
                FROM ccct_queries
                WHERE snapshot_id=%s
                """,
                (ccct.snapshot_id,),
            )
            query_count = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM ccct_requirements
                WHERE snapshot_id=%s
                """,
                (ccct.snapshot_id,),
            )
            requirement_count = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT count(*)
                FROM ccct_source_records
                WHERE snapshot_id=%s
                """,
                (ccct.snapshot_id,),
            )
            source_record_count = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT member_kind, count(*)
                FROM knowledge_generation_dataset_members
                WHERE generation_id=%s
                  AND dataset_id='ccct'
                  AND snapshot_id=%s
                GROUP BY member_kind
                """,
                (self.generation_id, ccct.snapshot_id),
            )
            member_counts = {str(kind): int(count) for kind, count in cur.fetchall()}
        expected_members = {
            "ccct_query": expected_queries,
            "ccct_requirement": expected_requirements,
            "ccct_source_record": expected_requirements,
            "ccct_document": expected_documents,
        }
        if (
            query_count != expected_queries
            or requirement_count != expected_requirements
            or source_record_count != expected_requirements
            or member_counts != expected_members
        ):
            raise GenerationEvidenceError("generation CCCT projection is incomplete")

    def _verify_external_stores(self, expected_articles: int) -> None:
        collection = qdrant_collection_name(self.generation_id)
        qdrant = self._qdrant_factory()
        if not qdrant.collection_exists(collection):
            raise GenerationEvidenceError("generation Qdrant collection is unavailable")
        if int(qdrant.count(collection, exact=True).count) != expected_articles:
            raise GenerationEvidenceError("generation Qdrant projection is incomplete")

        hsk = self._dataset("hsk")
        expected_hsk = int(hsk.metrics.get("leaf_node_count", -1))
        with self._connection_factory() as connection, connection.cursor() as cur:
            cur.execute(
                """
                SELECT d.source_definition, d.source_definition_sha256,
                       count(m.member_key), count(DISTINCT m.member_key)
                FROM knowledge_generation_datasets d
                LEFT JOIN knowledge_generation_dataset_members m
                  ON m.generation_id=d.generation_id
                 AND m.dataset_id=d.dataset_id
                 AND m.snapshot_id=d.snapshot_id
                 AND m.member_kind='hsk_leaf'
                WHERE d.generation_id=%s
                  AND d.dataset_id='hsk'
                  AND d.snapshot_id=%s
                GROUP BY d.source_definition, d.source_definition_sha256
                """,
                (self.generation_id, hsk.snapshot_id),
            )
            hsk_row = cur.fetchone()
        hsk_definition = hsk.as_dict()
        if (
            hsk_row is None
            or hsk_row[0] != hsk_definition
            or hsk_row[1] != canonical_sha256(hsk_definition)
            or int(hsk_row[2]) != expected_hsk
            or int(hsk_row[3]) != expected_hsk
        ):
            raise GenerationEvidenceError("generation HSK dataset identity is incomplete")
        driver = self._neo4j_factory()
        try:
            with driver.session() as session:
                row = session.run(
                    """
                    MATCH (g:KnowledgeGeneration {generation_id: $generation_id})
                    OPTIONAL MATCH (g)-[:CONTAINS]->(a:GenerationArticle {
                      generation_id: $generation_id
                    })
                    OPTIONAL MATCH (g)-[:CONTAINS_HSK {
                      generation_id: $generation_id,
                      snapshot_id: $hsk_snapshot_id
                    }]->(h:GenerationHSK {
                      generation_id: $generation_id,
                      snapshot_id: $hsk_snapshot_id
                    })
                    WHERE h.source_manifest_sha256=$source_manifest_sha256
                      AND h.model_manifest_sha256=$model_manifest_sha256
                      AND h.content_sha256 =~ '^[0-9a-f]{64}$'
                    RETURN count(DISTINCT a) AS article_count,
                           count(DISTINCT h) AS hsk_leaf_count,
                           count(DISTINCT h.code) AS hsk_code_count
                    """,
                    generation_id=self.generation_id,
                    hsk_snapshot_id=hsk.snapshot_id,
                    source_manifest_sha256=(self.identity.knowledge_source_manifest_sha256),
                    model_manifest_sha256=self.identity.model_manifest_sha256,
                ).single()
        finally:
            driver.close()
        if (
            row is None
            or int(row["article_count"]) != expected_articles
            or int(row["hsk_leaf_count"]) != expected_hsk
            or int(row["hsk_code_count"]) != expected_hsk
        ):
            raise GenerationEvidenceError("generation Neo4j projection is incomplete")

    def validate_ready(self) -> None:
        """Run all generation-scoped release checks; any legacy-only stack fails."""

        self._assert_postgres_pointer()
        self._manifest()
        counts = self._store_attestation_counts()
        expected = counts["postgresql"]
        self._verify_postgres_laws(expected)
        self._verify_ccct()
        self._verify_external_stores(expected)
        self._assert_postgres_pointer()

    def current_snapshot_id(self) -> str:
        ccct = self._dataset("ccct")
        if ccct.snapshot_id != self.identity.knowledge_snapshot_id:
            raise GenerationEvidenceError("generation CCCT snapshot identity mismatch")
        return ccct.snapshot_id

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        if snapshot_id != self.current_snapshot_id():
            raise GenerationEvidenceError("snapshot does not belong to generation")
        return self.identity.supported_review_as_of

    def resolve_hsk(self, code: str | None) -> HskResolution:
        try:
            normalized = normalize_hsk(code)
        except ValueError:
            return HskResolution("invalid", None, None)
        if normalized is None:
            return HskResolution("missing", None, None)
        driver = self._neo4j_factory()
        hsk_snapshot_id = self._dataset("hsk").snapshot_id
        try:
            with driver.session() as session:
                rows = session.run(
                    """
                    MATCH (:KnowledgeGeneration {generation_id: $generation_id})
                          -[:CONTAINS_HSK {
                            generation_id: $generation_id,
                            snapshot_id: $hsk_snapshot_id
                          }]->(h:GenerationHSK {
                            generation_id: $generation_id,
                            snapshot_id: $hsk_snapshot_id
                          })
                    WHERE h.source_manifest_sha256=$source_manifest_sha256
                      AND h.model_manifest_sha256=$model_manifest_sha256
                      AND (
                        h.code = $code OR
                        ($prefix IS NOT NULL AND h.code STARTS WITH $prefix)
                      )
                    RETURN h.code AS code,
                           h.content_sha256 AS content_sha256
                    ORDER BY code
                    """,
                    generation_id=self.generation_id,
                    hsk_snapshot_id=hsk_snapshot_id,
                    source_manifest_sha256=(self.identity.knowledge_source_manifest_sha256),
                    model_manifest_sha256=self.identity.model_manifest_sha256,
                    code=normalized,
                    prefix=normalized if len(normalized) == 6 else None,
                )
                projected = [(str(row["code"]), str(row["content_sha256"])) for row in rows]
        finally:
            driver.close()
        leaves = [code for code, _content_sha256 in projected]
        if len(leaves) != len(set(leaves)):
            raise GenerationEvidenceError("generation HSK projection contains duplicates")
        if projected:
            with self._connection_factory() as connection, connection.cursor() as cur:
                cur.execute(
                    """
                    SELECT member_key, payload_sha256
                    FROM knowledge_generation_dataset_members
                    WHERE generation_id=%s
                      AND dataset_id='hsk'
                      AND snapshot_id=%s
                      AND member_kind='hsk_leaf'
                      AND member_key = ANY(%s)
                    ORDER BY member_key
                    """,
                    (self.generation_id, hsk_snapshot_id, sorted(leaves)),
                )
                postgres_members = {
                    str(code): str(payload_sha256) for code, payload_sha256 in cur.fetchall()
                }
            if postgres_members != dict(projected):
                raise GenerationEvidenceError(
                    "Neo4j HSK result does not match sealed generation membership"
                )
        return resolve_hsk_candidates(normalized, leaves)

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: date,
    ) -> RequirementLookup:
        if snapshot_id != self.current_snapshot_id():
            raise GenerationEvidenceError("CCCT lookup escaped generation snapshot")
        with (
            self._connection_factory() as connection,
            connection.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute(
                """
                SELECT EXISTS (
                  SELECT 1
                  FROM ccct_queries q
                  JOIN knowledge_generation_dataset_members m
                    ON m.generation_id=%s
                   AND m.dataset_id='ccct'
                   AND m.snapshot_id=q.snapshot_id
                   AND m.member_kind='ccct_query'
                   AND m.member_key=q.hsk10 || ':' || q.trade_type
                  WHERE q.snapshot_id=%s AND q.hsk10=%s AND q.trade_type=%s
                ) AS covered
                """,
                (self.generation_id, snapshot_id, hsk10, trade_type),
            )
            covered = bool(cur.fetchone()["covered"])
            if not covered:
                return RequirementLookup(False, ())
            cur.execute(
                """
                SELECT r.requirement_id, r.requirement_version_hash, r.hsk10,
                       r.trade_type, r.law_code, r.law_name,
                       r.agency_code, r.agency_name,
                       r.document_code, r.document_name,
                       r.valid_from, r.valid_to,
                       src.source_record_id, src.source_uri
                FROM ccct_requirements r
                JOIN knowledge_generation_dataset_members rm
                  ON rm.generation_id=%s
                 AND rm.dataset_id='ccct'
                 AND rm.snapshot_id=r.snapshot_id
                 AND rm.member_kind='ccct_requirement'
                 AND rm.member_key=r.requirement_id
                JOIN LATERAL (
                  SELECT s.source_record_id, s.source_uri
                  FROM ccct_source_records s
                  JOIN knowledge_generation_dataset_members sm
                    ON sm.generation_id=%s
                   AND sm.dataset_id='ccct'
                   AND sm.snapshot_id=s.snapshot_id
                   AND sm.member_kind='ccct_source_record'
                   AND sm.member_key=s.source_record_id
                  WHERE s.snapshot_id=r.snapshot_id
                    AND s.requirement_id=r.requirement_id
                  ORDER BY s.source_record_id
                  LIMIT 1
                ) src ON true
                WHERE r.snapshot_id=%s AND r.hsk10=%s AND r.trade_type=%s
                  AND (r.valid_from IS NULL OR r.valid_from <= %s)
                  AND (r.valid_to IS NULL OR r.valid_to >= %s)
                ORDER BY r.law_code, r.document_code, r.agency_code, r.requirement_id
                """,
                (
                    self.generation_id,
                    self.generation_id,
                    snapshot_id,
                    hsk10,
                    trade_type,
                    as_of,
                    as_of,
                ),
            )
            return RequirementLookup(True, tuple(dict(row) for row in cur.fetchall()))

    def _qdrant_arm(self, query: str, arm: str, limit: int) -> Sequence[Any]:
        collection = qdrant_collection_name(self.generation_id)
        qdrant = self._qdrant_factory()
        generation_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="generation_id",
                    match=models.MatchValue(value=self.generation_id),
                )
            ]
        )
        if arm == DENSE_VECTOR:
            dense = self._dense_encoder([query])[0]
            vector = dense.tolist() if hasattr(dense, "tolist") else list(dense)
        elif arm == BM25_VECTOR:
            indices, values = self._sparse_encoder(query)
            vector = models.SparseVector(indices=indices, values=values)
        else:
            raise ValueError("unsupported retrieval arm")
        return qdrant.query_points(
            collection_name=collection,
            query=vector,
            using=arm,
            query_filter=generation_filter,
            limit=limit,
            with_payload=True,
        ).points

    def _graph_related(self, seed_ids: Sequence[int]) -> dict[int, float]:
        if not seed_ids:
            return {}
        driver = self._neo4j_factory()
        try:
            with driver.session() as session:
                rows = session.run(
                    """
                    UNWIND $seed_ids AS seed_id
                    MATCH (source:GenerationArticle {
                      generation_id: $generation_id,
                      article_pk: seed_id
                    })-[r:GENERATION_RELATION {
                      generation_id: $generation_id
                    }]-(target:GenerationArticle {
                      generation_id: $generation_id
                    })
                    RETURN target.article_pk AS article_pk, count(r) AS edge_count
                    """,
                    generation_id=self.generation_id,
                    seed_ids=list(seed_ids),
                )
                return {
                    int(row["article_pk"]): min(1.0, 0.1 * int(row["edge_count"])) for row in rows
                }
        finally:
            driver.close()

    def search_law_articles(
        self,
        query: str,
        top_k: int,
        as_of: date,
    ) -> tuple[LawArticleMatch, ...]:
        recall = min(max(top_k * 2, 8), 40)
        candidates: dict[int, dict[str, Any]] = {}
        rrf_k = 60
        for method in (DENSE_VECTOR, BM25_VECTOR):
            for rank, point in enumerate(self._qdrant_arm(query, method, recall), start=1):
                payload = getattr(point, "payload", None)
                if (
                    not isinstance(payload, dict)
                    or payload.get("generation_id") != self.generation_id
                    or payload.get("model_manifest_sha256") != self.identity.model_manifest_sha256
                ):
                    raise GenerationEvidenceError("Qdrant result escaped generation")
                try:
                    pk = int(payload["article_pk"])
                    content_sha256 = str(payload["content_sha256"])
                    law_id = str(payload["law_id"])
                    article_no = int(payload["article_no"])
                    paragraph_no = payload.get("paragraph_no")
                    if paragraph_no is not None:
                        paragraph_no = int(paragraph_no)
                    text = str(payload["text"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise GenerationEvidenceError("Qdrant generation payload is malformed") from exc
                if (
                    re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
                    or hashlib.sha256(text.encode("utf-8")).hexdigest() != content_sha256
                ):
                    raise GenerationEvidenceError("Qdrant generation content identity mismatch")
                projection = (
                    content_sha256,
                    law_id,
                    article_no,
                    paragraph_no,
                )
                entry = candidates.setdefault(pk, {"methods": set(), "raw": {}, "rrf": 0.0})
                previous_projection = entry.get("qdrant_projection")
                if previous_projection is not None and previous_projection != projection:
                    raise GenerationEvidenceError(
                        "Qdrant retrieval arms disagree on article identity"
                    )
                entry["qdrant_projection"] = projection
                entry["methods"].add(method)
                entry["raw"][method] = float(point.score)
                entry["rrf"] += 1.0 / (rrf_k + rank)
        seeds = sorted(candidates, key=lambda pk: candidates[pk]["rrf"], reverse=True)[:3]
        for pk, graph_score in self._graph_related(seeds).items():
            entry = candidates.setdefault(pk, {"methods": set(), "raw": {}, "rrf": 0.0})
            entry["methods"].add("graph")
            entry["raw"]["graph"] = graph_score
            entry["rrf"] += graph_score / (rrf_k + 1)
        if not candidates:
            return ()

        with (
            self._connection_factory() as connection,
            connection.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute(
                """
                SELECT a.article_pk, a.law_id, l.law_name,
                       CASE
                         WHEN l.law_name LIKE '%%시행규칙' THEN '시행규칙'
                         WHEN l.law_name LIKE '%%시행령' THEN '시행령'
                         ELSE '법률'
                       END AS hierarchy,
                       a.article_no, a.paragraph_no, a.item_no, a.title,
                       a.content, a.content_sha256 AS content_hash,
                       a.version_mst, a.valid_from, NULL::date AS valid_to
                FROM knowledge_generation_articles a
                JOIN knowledge_generation_laws l
                  ON l.generation_id=a.generation_id AND l.law_id=a.law_id
                WHERE a.generation_id=%s
                  AND a.article_pk = ANY(%s)
                  AND a.valid_from <= %s
                """,
                (self.generation_id, list(candidates), as_of),
            )
            rows = {int(row["article_pk"]): dict(row) for row in cur.fetchall()}
        if set(rows) != set(candidates):
            raise GenerationEvidenceError(
                "retrieval candidate is absent from immutable PostgreSQL generation"
            )

        query_tokens = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", query.lower()))
        theoretical_rrf_max = 2.0 / (rrf_k + 1)
        matches: list[LawArticleMatch] = []
        for pk, candidate in candidates.items():
            row = rows.get(pk)
            if row is None:  # guarded by the exact set comparison above
                raise GenerationEvidenceError("PostgreSQL generation candidate disappeared")
            projection = candidate.get("qdrant_projection")
            if projection is not None and projection != (
                row["content_hash"],
                row["law_id"],
                row["article_no"],
                row["paragraph_no"],
            ):
                raise GenerationEvidenceError(
                    "Qdrant candidate does not match immutable PostgreSQL content"
                )
            if (
                hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest()
                != row["content_hash"]
            ):
                raise GenerationEvidenceError("PostgreSQL generation content hash mismatch")
            row["source_uri"] = (
                "https://www.law.go.kr/법령/"
                f"{quote(str(row['law_name']))}/제{int(row['article_no']) // 100}조"
            )
            text_tokens = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", str(row["content"]).lower()))
            lexical = len(query_tokens & text_tokens) / len(query_tokens) if query_tokens else 0.0
            score = min(
                1.0,
                0.85 * min(1.0, candidate["rrf"] / theoretical_rrf_max) + 0.15 * lexical,
            )
            methods = tuple(
                method for method in ("dense", "bm25", "graph") if method in candidate["methods"]
            )
            matches.append(
                LawArticleMatch(
                    row=row,
                    retrieval_methods=methods,
                    score=score,
                    raw_scores={
                        **candidate["raw"],
                        "rrf": float(candidate["rrf"]),
                        "lexical_overlap": float(lexical),
                    },
                )
            )
        matches.sort(key=lambda item: (-item.score, item.row["article_pk"]))
        return tuple(matches[:top_k])


__all__ = ["GenerationEvidenceBackend", "GenerationEvidenceError"]
