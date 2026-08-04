"""Run all local UAT scenarios against the preserved actual three-law stores.

This runner is separate from ``run_three_law_v3_diagnostic.py``.  Both use the
same read-only PostgreSQL/Qdrant/Neo4j snapshot, while this profile binds a
distinct signed UAT-only ``human_only`` policy to one observed real CCCT
document code.  It is loopback-only and can never claim legal approval,
Production readiness, domain review, RAG quality, or automatic completion.
"""

from __future__ import annotations

import argparse
import contextvars
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import Request
from psycopg.rows import dict_row
from pydantic import SecretStr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Match the reviewed preserved-store runner before importing model libraries.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

ROOT = Path(__file__).resolve().parents[1]
for import_root in (ROOT, ROOT / "law_repository"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.db.qdrant import BM25_VECTOR, DENSE_VECTOR  # noqa: E402
from src.deadline import RequestDeadline  # noqa: E402
from src.evidence import LawArticleMatch, RequirementLookup  # noqa: E402
from src.evidence_v3 import RequirementPolicyBinding  # noqa: E402
from src.embed import rerank  # noqa: E402
from src.embed.model_identity import RERANK_MODEL  # noqa: E402
from src.model_runtime import DenseModelSupervisor  # noqa: E402
from src.neo4j_runtime import Neo4jQueryExecutor  # noqa: E402
from src.runtime_service import create_test_runtime_v3_application  # noqa: E402
from src.runtime_settings import RuntimeSettings  # noqa: E402
from src.runtime_api_v3 import (  # noqa: E402
    RUNTIME_V3_RETRIEVE_PATH,
    runtime_v3_readiness,
)
from src.runtime_release import GenerationRequestLease, RuntimeReleaseError  # noqa: E402
from src.runtime_release_v3 import (  # noqa: E402
    EvidenceRuntimeReleaseIdentityV3,
    RuntimeV3LeaseLoader,
    load_runtime_v3_contract_hashes,
)

from tools import run_three_law_v2_diagnostic as v2  # noqa: E402
from tools import run_three_law_v3_diagnostic as diagnostic_v3  # noqa: E402
from tools.run_evidence_v2_conformance_server import (  # noqa: E402
    VerifiedProviderRuntimeLock,
    load_bearer_token_file,
    load_verified_provider_runtime_lock,
)
from tools.run_evidence_v3_conformance_server import (  # noqa: E402
    write_test_only_v3_identity_json,
)
from tools.scenario_eval_v3_policy import (  # noqa: E402
    SCENARIO_EVAL_POLICY_MODE,
    SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
    build_scenario_eval_policy_fixture,
)

EVIDENCE_PROFILE = "preserved-3law-v3-scenario-eval"
EVIDENCE_SCOPE = "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58"
EVIDENCE_DATA_SOURCE = "ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY"
LITERAL_LOOPBACK_HOSTS = ("127.0.0.1", "::1")
EXPECTED_PRODUCTION_LAW_COUNT = v2.EXPECTED_PRODUCTION_LAW_COUNT
EXPECTED_SCENARIO_LAW_COUNT = v2.EXPECTED_DIAGNOSTIC_LAW_COUNT
EXPECTED_SCENARIO_ARTICLE_COUNT = diagnostic_v3.EXPECTED_DIAGNOSTIC_ARTICLE_COUNT
SCENARIO_EVAL_HSK10 = "0303540000"
SCENARIO_EVAL_TRADE_TYPE = "import"
SCENARIO_EVAL_READINESS_PATH = "/internal/rag/v3/health/scenario-eval-ready"
_MANIFEST_SCHEMA_VERSION = "three-law-evidence-v3-scenario-eval/v1"
_TRACE_SCHEMA_VERSION = "three-law-evidence-v3-scenario-eval-trace/v2"
# Local CPU inference needs enough room to finish the required BGE rerank and
# both fail-closed store observations that bind the response.  This is the
# configured runtime safety ceiling; it does not change candidates or ranking.
_SCENARIO_EVAL_REQUEST_DEADLINE_SECONDS = 300.0
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TRACE_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "three_law_scenario_eval_trace_context",
    default=None,
)
# Keep the required live rerank aligned with the repository's evaluated Hybrid
# path. Ten RRF candidates preserve the restricted Gold while bounding the
# 568M-parameter cross-encoder cost on a local CPU.
_RERANK_MIN_POOL = 10
_RERANK_MAX_POOL = 10
_BATCH_EXPAND_CYPHER = """
UNWIND $seed_pks AS requested_seed_pk
MATCH (a:Article {article_pk: requested_seed_pk})-[rel]->(t)
RETURN a.article_pk AS seed_article_pk,
       type(rel) AS rel, properties(rel) AS props,
       labels(t) AS labels, t.article_pk AS article_pk,
       t.law_id AS law_id, t.article_no AS article_no,
       coalesce(t.title, t.name) AS title
"""


class ThreeLawV3ScenarioEvalError(v2.ThreeLawDiagnosticError):
    """The scenario runner cannot prove its narrow read-only UAT boundary."""


def _require_reranker_ready(checker: Callable[[], bool]) -> None:
    """Reject scenario-eval startup when its attested reranker is unavailable."""

    try:
        ready = checker()
    except Exception as exc:
        raise ThreeLawV3ScenarioEvalError("scenario-eval reranker is unavailable") from exc
    if ready is not True:
        raise ThreeLawV3ScenarioEvalError("scenario-eval reranker is unavailable")


def _warm_reranker(scorer: Callable[[str, list[str]], list[float]]) -> None:
    """Prove one real inference before the request deadline starts."""

    values = _validated_rerank_scores(
        scorer,
        "통관 요건",
        ["통관 요건과 관련된 법령 근거"],
    )
    if len(values) != 1:
        raise ThreeLawV3ScenarioEvalError("scenario-eval reranker warm-up is incomplete")


def _validated_rerank_scores(
    scorer: Callable[[str, list[str]], list[float]],
    query: str,
    passages: list[str],
) -> list[float]:
    """Validate a strict score batch without exposing query or passage content."""

    try:
        values = scorer(query, passages)
    except Exception as exc:
        raise ThreeLawV3ScenarioEvalError("scenario-eval reranking failed") from exc
    try:
        complete = len(values) == len(passages)
    except TypeError as exc:
        raise ThreeLawV3ScenarioEvalError(
            "scenario-eval reranking returned an invalid batch"
        ) from exc
    if not complete:
        raise ThreeLawV3ScenarioEvalError("scenario-eval reranking returned an incomplete batch")
    try:
        normalized = [float(value) for value in values]
    except (TypeError, ValueError) as exc:
        raise ThreeLawV3ScenarioEvalError(
            "scenario-eval reranking returned an invalid score"
        ) from exc
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in normalized):
        raise ThreeLawV3ScenarioEvalError("scenario-eval reranking returned an invalid score")
    return normalized


def _store_state_projection(
    observation: v2.ThreeLawObservation,
) -> dict[str, int | str]:
    """Return the credential-free store state revalidated by each readiness call."""

    projection: dict[str, int | str] = {
        "knowledge_snapshot_id": observation.knowledge_snapshot_id,
        "postgres_article_count": observation.article_count,
        "qdrant_point_count": observation.qdrant_point_count,
        "neo4j_article_count": observation.neo4j_article_count,
        "postgres_retrieval_state_sha256": (observation.postgres_retrieval_state_sha256),
        "qdrant_retrieval_state_sha256": observation.qdrant_retrieval_state_sha256,
        "neo4j_retrieval_state_sha256": observation.neo4j_retrieval_state_sha256,
    }
    projection["combined_state_sha256"] = hashlib.sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return projection


def _validate_exact_observation(
    observation: v2.ThreeLawObservation,
    *,
    expected_loaded_law_count: int,
) -> None:
    try:
        diagnostic_v3._validate_exact_observation(  # noqa: SLF001 - shared reviewed boundary
            observation,
            expected_loaded_law_count=expected_loaded_law_count,
        )
    except Exception as exc:
        raise ThreeLawV3ScenarioEvalError(str(exc)) from exc


def attest_executing_scenario_eval_repository(
    provider_lock: VerifiedProviderRuntimeLock,
    *,
    repository: Path = ROOT,
    deadline: RequestDeadline | None = None,
) -> None:
    """Bind the V2 actual-store backend and both new UAT modules to one SHA."""

    v2.attest_executing_repository(
        provider_lock,
        repository=repository,
        deadline=deadline,
    )
    for source in (Path(__file__), ROOT / "tools" / "scenario_eval_v3_policy.py"):
        relative = source.resolve().relative_to(repository.resolve()).as_posix()
        committed = v2._git(  # noqa: SLF001 - same bounded Git attestor
            repository,
            "show",
            f"HEAD:{relative}",
            deadline=deadline,
            text=False,
        )
        if committed != source.read_bytes():
            raise ThreeLawV3ScenarioEvalError(
                "executing scenario-eval bytes do not match the provider revision"
            )


def _validate_new_output_path(
    output: Path,
    *,
    context: Path,
    label: str,
) -> Path:
    try:
        return diagnostic_v3._validate_new_output_path(  # noqa: SLF001
            output,
            context=context,
            label=label,
        )
    except Exception as exc:
        raise ThreeLawV3ScenarioEvalError(str(exc)) from exc


@dataclass(slots=True)
class _StageTrace:
    attempt_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    result_count: int = 0
    duration_ms: int = 0
    top_k_result_count: int = 0

    def record(self, *, success: bool, result_count: int, duration_ms: int) -> None:
        self.attempt_count += 1
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1
        self.result_count += max(0, result_count)
        self.duration_ms += max(0, duration_ms)

    def to_wire(self) -> dict[str, object]:
        return {
            "attempted": self.attempt_count > 0,
            "succeeded": (
                self.attempt_count > 0
                and self.failure_count == 0
                and self.success_count == self.attempt_count
            ),
            "attempt_count": self.attempt_count,
            "result_count": self.result_count,
            "top_k_result_count": self.top_k_result_count,
            "duration_ms": self.duration_ms,
        }


@dataclass(slots=True)
class _RequestTrace:
    stages: dict[str, _StageTrace] = field(
        default_factory=lambda: {
            name: _StageTrace() for name in ("structured", "dense", "bm25", "rerank", "graph")
        }
    )


class ScenarioEvalTraceCollector:
    """Thread-safe bounded request-stage counters; it never accepts content."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[str, _RequestTrace] = {}

    def begin(self, key: str) -> None:
        with self._lock:
            if key in self._requests:
                raise ThreeLawV3ScenarioEvalError("duplicate trace context")
            self._requests[key] = _RequestTrace()

    def record(
        self,
        stage: str,
        *,
        success: bool,
        result_count: int,
        duration_ms: int,
    ) -> None:
        key = _TRACE_CONTEXT.get()
        if key is None:
            return
        with self._lock:
            trace = self._requests.get(key)
            if trace is None or stage not in trace.stages:
                raise ThreeLawV3ScenarioEvalError("request trace context is unavailable")
            trace.stages[stage].record(
                success=success,
                result_count=result_count,
                duration_ms=duration_ms,
            )

    def top_k(self, stage: str, count: int) -> None:
        key = _TRACE_CONTEXT.get()
        if key is None:
            return
        with self._lock:
            trace = self._requests.get(key)
            if trace is None or stage not in trace.stages:
                raise ThreeLawV3ScenarioEvalError("request trace context is unavailable")
            trace.stages[stage].top_k_result_count += max(0, count)

    def finish(self, key: str) -> dict[str, dict[str, object]]:
        with self._lock:
            trace = self._requests.pop(key, None)
        if trace is None:
            raise ThreeLawV3ScenarioEvalError("request trace was not started")
        return {name: value.to_wire() for name, value in trace.stages.items()}

    def abandon(self, key: str) -> None:
        with self._lock:
            self._requests.pop(key, None)


class SafeScenarioEvalTraceWriter:
    """Append-only JSONL writer created outside source and provider contexts."""

    def __init__(self, destination: Path) -> None:
        self.path = destination
        self._lock = threading.Lock()
        self._closed = False
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(destination, flags, 0o600)
            opened = os.fstat(descriptor)
            linked = os.stat(destination, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
                raise OSError("trace output identity changed")
            self._stream = os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
            descriptor = None
            os.chmod(destination, 0o600)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            destination.unlink(missing_ok=True)
            raise ThreeLawV3ScenarioEvalError("trace output could not be created") from exc

    def write(self, payload: Mapping[str, object]) -> None:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(serialized.encode("utf-8")) > 32 * 1024:
            raise ThreeLawV3ScenarioEvalError("trace record exceeds its safe size limit")
        with self._lock:
            if self._closed:
                raise ThreeLawV3ScenarioEvalError("trace output is closed")
            self._stream.write(serialized + "\n")
            self._stream.flush()
            os.fsync(self._stream.fileno())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._stream.close()
            self._closed = True


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1_000))


class ScenarioEvalDatabaseBackend(v2.ThreeLawDiagnosticDatabaseBackend):
    """Actual three-store backend with content-free per-arm observations."""

    def __init__(
        self,
        *args: Any,
        trace_collector: ScenarioEvalTraceCollector,
        rerank_scorer: Callable[[str, list[str]], list[float]],
        reranker_readiness: Callable[[], bool],
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self._scenario_trace_collector = trace_collector
        self._scenario_rerank_scorer = rerank_scorer
        self._scenario_reranker_readiness = reranker_readiness

    def validate_ready(self) -> None:
        super().validate_ready()
        _require_reranker_ready(self._scenario_reranker_readiness)

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: Any,
    ) -> RequirementLookup:
        started = time.monotonic()
        try:
            result = super().lookup_requirements(snapshot_id, hsk10, trade_type, as_of)
        except Exception:
            self._scenario_trace_collector.record(
                "structured",
                success=False,
                result_count=0,
                duration_ms=_duration_ms(started),
            )
            raise
        self._scenario_trace_collector.record(
            "structured",
            success=True,
            result_count=len(result.rows),
            duration_ms=_duration_ms(started),
        )
        return result

    def _arm_hits(
        self,
        query: str,
        arm: str,
        recall: int,
        as_of: Any,
    ) -> list[Any]:
        stage = {DENSE_VECTOR: "dense", BM25_VECTOR: "bm25"}.get(arm)
        if stage is None:
            return super()._arm_hits(query, arm, recall, as_of)
        started = time.monotonic()
        try:
            hits = super()._arm_hits(query, arm, recall, as_of)
        except Exception:
            self._scenario_trace_collector.record(
                stage,
                success=False,
                result_count=0,
                duration_ms=_duration_ms(started),
            )
            raise
        self._scenario_trace_collector.record(
            stage,
            success=True,
            result_count=len(hits),
            duration_ms=_duration_ms(started),
        )
        return hits

    def _run_neo4j(
        self,
        query: str,
        parameters: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        # HSK resolution and snapshot checks also use Neo4j, but the public
        # "graph" stage means the one-hop law expansion specifically.  Keeping
        # those meanings separate prevents a successful HSK lookup from being
        # reported as successful graph expansion.
        if query not in {
            v2.repository_module._EXPAND_CYPHER,  # noqa: SLF001
            _BATCH_EXPAND_CYPHER,
        }:
            return super()._run_neo4j(query, parameters)
        started = time.monotonic()
        try:
            rows = super()._run_neo4j(query, parameters)
        except Exception:
            self._scenario_trace_collector.record(
                "graph",
                success=False,
                result_count=0,
                duration_ms=_duration_ms(started),
            )
            raise
        self._scenario_trace_collector.record(
            "graph",
            success=True,
            result_count=len(rows),
            duration_ms=_duration_ms(started),
        )
        return rows

    def _law_rows(self, article_pks: list[int], as_of: Any) -> dict[int, dict[str, Any]]:
        with self._connection() as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """
                SELECT a.id AS article_pk, a.law_id, l.law_name, l.hierarchy,
                       a.article_no, a.paragraph_no, a.item_no, a.title, a.content,
                       a.content_hash, a.version_mst, a.valid_from, a.valid_to
                FROM law_articles a JOIN laws l USING (law_id)
                WHERE a.id = ANY(%s)
                  AND a.valid_from <= %s
                  AND (a.valid_to IS NULL OR a.valid_to > %s)
                """,
                (article_pks, as_of, as_of),
            )
            return {int(row["article_pk"]): dict(row) for row in cursor.fetchall()}

    def _rerank(
        self,
        query: str,
        article_pks: list[int],
        passages: list[str],
    ) -> dict[int, float]:
        started = time.monotonic()
        try:
            values = _validated_rerank_scores(
                self._scenario_rerank_scorer,
                query,
                passages,
            )
        except Exception:
            self._scenario_trace_collector.record(
                "rerank",
                success=False,
                result_count=0,
                duration_ms=_duration_ms(started),
            )
            raise
        self._scenario_trace_collector.record(
            "rerank",
            success=True,
            result_count=len(values),
            duration_ms=_duration_ms(started),
        )
        return dict(zip(article_pks, values, strict=True))

    def search_law_articles(
        self,
        query: str,
        top_k: int,
        as_of: Any,
    ) -> tuple[LawArticleMatch, ...]:
        """Dense+BM25 RRF -> required BGE rerank -> graph -> final ranking."""

        self._require_deadline()
        recall = min(max(top_k * 2, 8), _RERANK_MAX_POOL)
        dense_hits = self._arm_hits(query, DENSE_VECTOR, recall, as_of)
        bm25_hits = self._arm_hits(query, BM25_VECTOR, recall, as_of)
        rrf_k = 60
        candidates: dict[int, dict[str, Any]] = {}
        texts: dict[int, str] = {}
        for method, hits in (("dense", dense_hits), ("bm25", bm25_hits)):
            for rank, hit in enumerate(hits, start=1):
                entry = candidates.setdefault(
                    hit.article_pk,
                    {"methods": set(), "raw": {}, "rrf": 0.0},
                )
                entry["methods"].add(method)
                entry["raw"][method] = float(hit.score)
                entry["rrf"] += 1.0 / (rrf_k + rank)
                existing_text = texts.setdefault(hit.article_pk, str(hit.text))
                if existing_text != str(hit.text):
                    raise ThreeLawV3ScenarioEvalError(
                        "retrieval arms disagree on law article content"
                    )

        if not candidates:
            return ()

        rrf_order = sorted(
            candidates,
            key=lambda article_pk: (-float(candidates[article_pk]["rrf"]), article_pk),
        )
        pool_size = min(
            len(rrf_order),
            max(_RERANK_MIN_POOL, min(top_k * 2, _RERANK_MAX_POOL)),
        )
        initial_pool = rrf_order[:pool_size]
        candidates = {article_pk: candidates[article_pk] for article_pk in initial_pool}
        reranker_scores = self._rerank(
            query,
            initial_pool,
            [texts[article_pk] for article_pk in initial_pool],
        )

        seeds = sorted(
            initial_pool,
            key=lambda article_pk: (
                -reranker_scores[article_pk],
                -float(candidates[article_pk]["rrf"]),
                article_pk,
            ),
        )[:3]
        related_rows = self._run_neo4j(
            _BATCH_EXPAND_CYPHER,
            {"seed_pks": seeds},
        )
        rows_by_seed: dict[int, list[dict[str, Any]]] = {seed_pk: [] for seed_pk in seeds}
        for related in related_rows:
            seed_article_pk = related.get("seed_article_pk")
            if type(seed_article_pk) is not int or seed_article_pk not in rows_by_seed:
                raise ThreeLawV3ScenarioEvalError(
                    "graph expansion result is not bound to a requested seed"
                )
            rows_by_seed[seed_article_pk].append(related)

        for seed_pk in seeds:
            related_rows_for_seed = rows_by_seed[seed_pk]
            for related in related_rows_for_seed:
                related_pk = related.get("article_pk")
                if not isinstance(related_pk, int) or related_pk == seed_pk:
                    continue
                entry = candidates.setdefault(
                    related_pk,
                    {
                        "methods": set(),
                        "raw": {},
                        "rrf": float(candidates[seed_pk]["rrf"]) * 0.65,
                    },
                )
                entry["methods"].add("graph")
                entry["raw"]["graph"] = max(
                    float(entry["raw"].get("graph", 0.0)),
                    float(candidates[seed_pk]["rrf"]),
                )

        rows = self._law_rows(list(candidates), as_of)
        if set(rows) != set(candidates):
            raise ThreeLawV3ScenarioEvalError(
                "retrieval candidate is absent from the attested PostgreSQL snapshot"
            )

        graph_only = [article_pk for article_pk in candidates if article_pk not in reranker_scores]
        if graph_only:
            graph_scores = self._rerank(
                query,
                graph_only,
                [str(rows[article_pk]["content"]) for article_pk in graph_only],
            )
            reranker_scores.update(graph_scores)

        query_tokens = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", query.lower()))
        matches: list[LawArticleMatch] = []
        for article_pk, candidate in candidates.items():
            row = rows[article_pk]
            text_tokens = set(re.findall(r"[0-9A-Za-z가-힣]{2,}", str(row["content"]).lower()))
            lexical = len(query_tokens & text_tokens) / len(query_tokens) if query_tokens else 0.0
            reranker_score = float(reranker_scores[article_pk])
            methods = cast(
                tuple[Literal["dense", "bm25", "graph"], ...],
                tuple(
                    method
                    for method in ("dense", "bm25", "graph")
                    if method in candidate["methods"]
                ),
            )
            matches.append(
                LawArticleMatch(
                    row=row,
                    retrieval_methods=methods,
                    score=reranker_score,
                    raw_scores={
                        **candidate["raw"],
                        "rrf": float(candidate["rrf"]),
                        "lexical_overlap": float(lexical),
                    },
                )
            )
        matches.sort(
            key=lambda match: (
                -match.score,
                -float(match.raw_scores["rrf"]),
                int(match.row["article_pk"]),
            )
        )
        matches = matches[:top_k]
        self._scenario_trace_collector.top_k("rerank", len(matches))
        for stage in ("dense", "bm25", "graph"):
            count = sum(stage in match.retrieval_methods for match in matches)
            self._scenario_trace_collector.top_k(stage, count)
        self._require_deadline()
        return tuple(matches)


class _TracingBackendProxy:
    """Test-factory adapter; the live path uses ``ScenarioEvalDatabaseBackend``."""

    def __init__(self, delegate: Any, trace_collector: ScenarioEvalTraceCollector) -> None:
        self._delegate = delegate
        self._trace_collector = trace_collector

    def validate_ready(self) -> None:
        self._delegate.validate_ready()

    def current_snapshot_id(self) -> str | None:
        return self._delegate.current_snapshot_id()

    def snapshot_effective_date(self, snapshot_id: str) -> Any:
        return self._delegate.snapshot_effective_date(snapshot_id)

    def resolve_hsk(self, code: str | None) -> Any:
        return self._delegate.resolve_hsk(code)

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: Any,
    ) -> RequirementLookup:
        started = time.monotonic()
        try:
            result = self._delegate.lookup_requirements(snapshot_id, hsk10, trade_type, as_of)
        except Exception:
            self._trace_collector.record(
                "structured",
                success=False,
                result_count=0,
                duration_ms=_duration_ms(started),
            )
            raise
        self._trace_collector.record(
            "structured",
            success=True,
            result_count=len(result.rows),
            duration_ms=_duration_ms(started),
        )
        return result

    def search_law_articles(
        self,
        query: str,
        top_k: int,
        as_of: Any,
    ) -> tuple[LawArticleMatch, ...]:
        started = time.monotonic()
        try:
            matches = self._delegate.search_law_articles(query, top_k, as_of)
        except Exception:
            duration = _duration_ms(started)
            for stage in ("dense", "bm25", "graph"):
                self._trace_collector.record(
                    stage,
                    success=False,
                    result_count=0,
                    duration_ms=duration,
                )
            raise
        duration = _duration_ms(started)
        for stage in ("dense", "bm25", "graph"):
            count = sum(stage in match.retrieval_methods for match in matches)
            self._trace_collector.record(
                stage,
                success=True,
                result_count=count,
                duration_ms=duration,
            )
            self._trace_collector.top_k(stage, count)
        reranked_count = sum("reranker" in match.raw_scores for match in matches)
        self._trace_collector.record(
            "rerank",
            success=reranked_count == len(matches),
            result_count=reranked_count,
            duration_ms=duration,
        )
        self._trace_collector.top_k("rerank", reranked_count)
        return matches


class ScenarioEvalTraceMiddleware:
    """Write one safe trace row for every V3 retrieval HTTP request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        collector: ScenarioEvalTraceCollector,
        writer: SafeScenarioEvalTraceWriter,
        identity: Mapping[str, object],
        knowledge_snapshot_id: str,
    ) -> None:
        self.app = app
        self.collector = collector
        self.writer = writer
        self.identity = dict(identity)
        self.knowledge_snapshot_id = knowledge_snapshot_id

    @staticmethod
    def _request_id_from_headers(headers: list[tuple[bytes, bytes]]) -> str | None:
        values = [value for name, value in headers if name.lower() == b"x-request-id"]
        if len(values) != 1:
            return None
        try:
            decoded = values[0].decode("ascii")
        except UnicodeDecodeError:
            return None
        return decoded if _REQUEST_ID_PATTERN.fullmatch(decoded) else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != RUNTIME_V3_RETRIEVE_PATH
        ):
            await self.app(scope, receive, send)
            return

        trace_key = uuid.uuid4().hex
        self.collector.begin(trace_key)
        token = _TRACE_CONTEXT.set(trace_key)
        input_request_id = self._request_id_from_headers(scope.get("headers", []))
        trace_written = False

        async def traced_send(message: Message) -> None:
            nonlocal trace_written
            if message["type"] == "http.response.start" and not trace_written:
                response_headers = list(message.get("headers", []))
                response_request_id = self._request_id_from_headers(response_headers)
                service_request_values = [
                    value
                    for name, value in response_headers
                    if name.lower() == b"x-service-request-id"
                ]
                service_request_id = None
                if len(service_request_values) == 1:
                    try:
                        candidate = service_request_values[0].decode("ascii")
                    except UnicodeDecodeError:
                        candidate = ""
                    if _REQUEST_ID_PATTERN.fullmatch(candidate):
                        service_request_id = candidate
                stages = self.collector.finish(trace_key)
                self.writer.write(
                    {
                        "schema_version": _TRACE_SCHEMA_VERSION,
                        "request_id": (
                            response_request_id
                            or input_request_id
                            or service_request_id
                            or "unavailable"
                        ),
                        "http_status_code": int(message["status"]),
                        "evidence_profile": EVIDENCE_PROFILE,
                        "evidence_scope": EVIDENCE_SCOPE,
                        "evidence_data_source": EVIDENCE_DATA_SOURCE,
                        "evidence_policy_mode": SCENARIO_EVAL_POLICY_MODE,
                        "knowledge_snapshot_id": self.knowledge_snapshot_id,
                        "provider_identity": self.identity,
                        "retrieval": stages,
                    }
                )
                trace_written = True
            await send(message)

        try:
            await self.app(scope, receive, traced_send)
        finally:
            _TRACE_CONTEXT.reset(token)
            if not trace_written:
                self.collector.abandon(trace_key)


def _validate_ccct_preflight(
    backend: Any,
    observation: v2.ThreeLawObservation,
) -> None:
    """Require the real structured lookup that the UAT fixture is scoped to."""

    backend.validate_ready()
    resolution = backend.resolve_hsk(SCENARIO_EVAL_HSK10)
    if (
        resolution.status != "exact"
        or resolution.resolved_hsk10 != SCENARIO_EVAL_HSK10
        or tuple(resolution.candidate_hsk10s) != (SCENARIO_EVAL_HSK10,)
    ):
        raise ThreeLawV3ScenarioEvalError("scenario-eval HSK preflight is not exact")
    lookup = backend.lookup_requirements(
        observation.knowledge_snapshot_id,
        SCENARIO_EVAL_HSK10,
        SCENARIO_EVAL_TRADE_TYPE,
        observation.supported_review_as_of,
    )
    document_codes = {
        str(row.get("document_code"))
        for row in lookup.rows
        if isinstance(row, Mapping) and isinstance(row.get("document_code"), str)
    }
    if not lookup.covered or SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE not in document_codes:
        raise ThreeLawV3ScenarioEvalError(
            "scenario-eval CCCT preflight required document is unavailable"
        )


@dataclass(frozen=True, slots=True)
class ThreeLawV3ScenarioEvalManifest:
    data_revision: str
    observation: v2.ThreeLawObservation
    identity: EvidenceRuntimeReleaseIdentityV3
    policy_binding: RequirementPolicyBinding

    def to_wire(self) -> dict[str, object]:
        base = v2.ThreeLawDiagnosticManifest(
            data_revision=self.data_revision,
            observation=self.observation,
            identity=self.identity.base_runtime_release,
        ).to_wire()
        base.pop("schema_version")
        base.pop("scope")
        base.pop("evidence_runtime_release")
        return {
            "schema_version": _MANIFEST_SCHEMA_VERSION,
            "evidence_profile": EVIDENCE_PROFILE,
            "scope": EVIDENCE_SCOPE,
            "evidence_scope": EVIDENCE_SCOPE,
            "evidence_data_source": EVIDENCE_DATA_SOURCE,
            "evidence_policy_mode": SCENARIO_EVAL_POLICY_MODE,
            **base,
            "law_ids": list(self.observation.law_ids),
            "expected_scenario_article_count": EXPECTED_SCENARIO_ARTICLE_COUNT,
            "ccct_preflight": {
                "hsk10": SCENARIO_EVAL_HSK10,
                "trade_type": SCENARIO_EVAL_TRADE_TYPE,
                "required_document_code": SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
                "present": True,
            },
            "test_policy": {
                "approved_fixture_signature": True,
                "verification_policy": "human_only",
                "legal_approval": False,
                "production_authority": False,
            },
            "test_policy_binding": self.policy_binding.model_dump(mode="json"),
            "store_state": _store_state_projection(self.observation),
            "domain_reviewed": False,
            "release_ready": False,
            "automatic_completion_eligible": False,
            "retrieval_runtime": {
                "postgresql": "structured-ledger-read-only",
                "qdrant": ["bge-m3-dense", "bm25-sparse", "rrf"],
                "reranker": {
                    "model_id": RERANK_MODEL.model_id,
                    "revision": RERANK_MODEL.revision,
                    "mode": "required-fail-closed",
                },
                "neo4j": "one-hop-law-and-requirement-expansion",
            },
            "trace_schema_version": _TRACE_SCHEMA_VERSION,
            "evidence_runtime_release_v3": self.identity.model_dump(mode="json"),
        }


def _trace_identity(identity: EvidenceRuntimeReleaseIdentityV3) -> dict[str, object]:
    base = identity.base_runtime_release
    return {
        "provider_release": base.provider_release,
        "provider_revision": base.provider_revision,
        "provider_build_sha256": base.provider_build_sha256,
        "knowledge_generation_id": base.knowledge_generation_id,
        "knowledge_generation_attestation_sha256": (base.knowledge_generation_attestation_sha256),
        "knowledge_source_manifest_sha256": base.knowledge_source_manifest_sha256,
        "supported_review_as_of": base.supported_review_as_of.isoformat(),
        "runtime_release_sha256": base.runtime_release_sha256,
        "runtime_release_v3_sha256": identity.runtime_release_v3_sha256,
        "requirement_policy_release": identity.requirement_policy_release,
        "requirement_policy_manifest_sha256": identity.requirement_policy_manifest_sha256,
        "requirement_policy_approval_sha256": identity.requirement_policy_approval_sha256,
        "reviewer_trust_store_sha256": identity.reviewer_trust_store_sha256,
    }


def create_three_law_v3_scenario_eval_application(
    *,
    scenario_eval_only_ack: bool,
    provider_context: Path,
    service_token_file: Path,
    identity_output: Path,
    scenario_manifest_output: Path,
    trace_output: Path,
    host: str,
    port: int,
    expected_loaded_law_count: int = EXPECTED_SCENARIO_LAW_COUNT,
    observation_loader: v2.ObservationLoader | None = None,
    provider_lock_loader: Callable[
        [Path],
        VerifiedProviderRuntimeLock,
    ] = load_verified_provider_runtime_lock,
    database_backend_factory: Callable[
        [v2.ThreeLawObservation, RequestDeadline],
        Any,
    ]
    | None = None,
    executing_repository_attestor: v2.ExecutingRepositoryAttestor | None = None,
    neo4j_query_executor: Neo4jQueryExecutor | None = None,
    model_supervisor: DenseModelSupervisor | None = None,
    rerank_scorer: Callable[[str, list[str]], list[float]] | None = None,
    reranker_readiness: Callable[[], bool] | None = None,
    install_readonly_boundary: bool = True,
) -> Any:
    """Create the fail-closed actual-store scenario evaluation service."""

    if not scenario_eval_only_ack:
        raise ThreeLawV3ScenarioEvalError(
            "explicit preserved three-law scenario-eval acknowledgement is required"
        )
    if host not in LITERAL_LOOPBACK_HOSTS:
        raise ThreeLawV3ScenarioEvalError("host must be loopback-only")
    if not 1 <= port <= 65_535:
        raise ThreeLawV3ScenarioEvalError("port must be from 1 to 65535")
    if expected_loaded_law_count != EXPECTED_SCENARIO_LAW_COUNT:
        raise ThreeLawV3ScenarioEvalError("expected loaded law count must be exactly 3")

    context = provider_context.resolve()
    identity_destination = _validate_new_output_path(
        identity_output,
        context=context,
        label="scenario-eval identity output",
    )
    manifest_destination = _validate_new_output_path(
        scenario_manifest_output,
        context=context,
        label="scenario-eval manifest output",
    )
    trace_destination = _validate_new_output_path(
        trace_output,
        context=context,
        label="scenario-eval trace output",
    )
    if len({identity_destination, manifest_destination, trace_destination}) != 3:
        raise ThreeLawV3ScenarioEvalError("scenario-eval output files must be different")

    provider_lock = provider_lock_loader(context)
    if (
        v2._REVISION_PATTERN.fullmatch(provider_lock.provider_revision) is None  # noqa: SLF001
        or provider_lock.provider_release != "evidence-api-v1.2.0"
    ):
        raise ThreeLawV3ScenarioEvalError("provider runtime lock is not a valid V3 base revision")
    selected_attestor = executing_repository_attestor or (
        lambda deadline=None: attest_executing_scenario_eval_repository(
            provider_lock,
            deadline=deadline,
        )
    )
    attestation_deadline = v2._deadline()  # noqa: SLF001 - bounded source attestation
    selected_attestor(attestation_deadline)
    token = load_bearer_token_file(service_token_file)

    readonly_boundary: v2._ReadOnlyPostgresBoundary | None = None  # noqa: SLF001
    created_neo4j_executor = False
    shared_neo4j_executor = neo4j_query_executor
    created_model_supervisor = False
    shared_model_supervisor = model_supervisor
    selected_rerank_scorer = rerank_scorer or rerank.required_scores
    selected_reranker_readiness = reranker_readiness or rerank.available
    trace_collector = ScenarioEvalTraceCollector()
    trace_writer: SafeScenarioEvalTraceWriter | None = None
    wrote_identity = False
    wrote_manifest = False
    try:
        if install_readonly_boundary:
            readonly_boundary = v2._ReadOnlyPostgresBoundary()  # noqa: SLF001
        if observation_loader is None or database_backend_factory is None:
            if shared_neo4j_executor is None:
                shared_neo4j_executor = v2._new_neo4j_executor()  # noqa: SLF001
                created_neo4j_executor = True
        if observation_loader is None:
            if shared_neo4j_executor is None:
                raise ThreeLawV3ScenarioEvalError("bounded Neo4j observer is unavailable")

            def selected_observation_loader(
                deadline: RequestDeadline | None = None,
            ) -> v2.ThreeLawObservation:
                return v2.observe_three_law_data(
                    deadline,
                    neo4j_query_executor=shared_neo4j_executor,
                )

        else:
            selected_observation_loader = observation_loader

        selected_database_backend_factory: Callable[
            [v2.ThreeLawObservation, RequestDeadline],
            Any,
        ]
        if database_backend_factory is None:
            if shared_neo4j_executor is None:
                raise ThreeLawV3ScenarioEvalError("bounded Neo4j backend is unavailable")
            if shared_model_supervisor is None:
                shared_model_supervisor = DenseModelSupervisor(
                    startup_timeout_seconds=180,
                    termination_timeout_seconds=0.5,
                )
                created_model_supervisor = True
            _require_reranker_ready(selected_reranker_readiness)
            _warm_reranker(selected_rerank_scorer)

            def selected_database_backend_factory(
                observation: v2.ThreeLawObservation,
                request_deadline: RequestDeadline,
            ) -> ScenarioEvalDatabaseBackend:
                if shared_neo4j_executor is None or shared_model_supervisor is None:
                    raise ThreeLawV3ScenarioEvalError(
                        "bounded scenario-eval dependencies are unavailable"
                    )
                return ScenarioEvalDatabaseBackend(
                    observation,
                    deadline=request_deadline,
                    neo4j_query_executor=shared_neo4j_executor,
                    dense_encoder=shared_model_supervisor.encode,
                    trace_collector=trace_collector,
                    rerank_scorer=selected_rerank_scorer,
                    reranker_readiness=selected_reranker_readiness,
                )

        else:

            def selected_database_backend_factory(
                observation: v2.ThreeLawObservation,
                request_deadline: RequestDeadline,
            ) -> _TracingBackendProxy:
                return _TracingBackendProxy(
                    database_backend_factory(observation, request_deadline),
                    trace_collector,
                )

        # A cold CPU reranker can spend most of the bounded model-startup
        # window loading its weights.  Store attestation must receive a fresh
        # deadline after that one-time warm-up; otherwise a healthy preserved
        # corpus is rejected before its first read-only observation begins.
        startup_deadline = v2._deadline()  # noqa: SLF001 - bounded store observation
        initial_observation = selected_observation_loader(startup_deadline)
        _validate_exact_observation(
            initial_observation,
            expected_loaded_law_count=expected_loaded_law_count,
        )
        preflight_backend = selected_database_backend_factory(
            initial_observation,
            startup_deadline,
        )
        _validate_ccct_preflight(preflight_backend, initial_observation)
        post_preflight_observation = selected_observation_loader(v2._deadline())  # noqa: SLF001
        _validate_exact_observation(
            post_preflight_observation,
            expected_loaded_law_count=expected_loaded_law_count,
        )
        if post_preflight_observation != initial_observation:
            raise ThreeLawV3ScenarioEvalError(
                "preserved three-law stores changed during scenario preflight"
            )

        base_lease = v2.derive_three_law_runtime_lease(provider_lock, initial_observation)
        base_loader = v2._ObservedDiagnosticRuntimeLeaseLoader(  # noqa: SLF001
            base_lease,
            initial_observation=initial_observation,
            observation_loader=selected_observation_loader,
            executing_repository_attestor=selected_attestor,
            expected_loaded_law_count=expected_loaded_law_count,
        )
        policy_fixture = build_scenario_eval_policy_fixture(
            knowledge_source_manifest_sha256=(base_lease.identity.knowledge_source_manifest_sha256),
            knowledge_snapshot_id=base_lease.identity.knowledge_snapshot_id,
        )
        bindings = policy_fixture.verified.bindings()
        binding = bindings.get(SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE)
        if (
            set(bindings) != {SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE}
            or binding is None
            or not binding.approved
            or binding.verification_policy != "human_only"
        ):
            raise ThreeLawV3ScenarioEvalError("scenario-eval policy scope is invalid")
        runtime_loader = RuntimeV3LeaseLoader(
            base_loader=base_loader,
            policy_loader=lambda: policy_fixture.verified,
            contract_hash_loader=lambda: load_runtime_v3_contract_hashes(context),
        )
        runtime_lease = runtime_loader.acquire(startup_deadline)
        if len(runtime_lease.identity.binding_headers()) != 17:
            raise ThreeLawV3ScenarioEvalError(
                "scenario-eval identity must expose exactly 17 bindings"
            )

        service_settings = RuntimeSettings(
            _env_file=None,
            service_token=SecretStr(token),
            service_token_file=None,
            transport_mode="loopback_dev",
            trusted_proxy_cidrs="",
            allow_insecure_loopback_dev=True,
            bind_host=host,
            port=port,
            max_concurrency=1,
            request_deadline_seconds=_SCENARIO_EVAL_REQUEST_DEADLINE_SECONDS,
            store_operation_timeout_seconds=20,
            store_termination_timeout_seconds=0.5,
            model_inference_timeout_seconds=20,
            model_termination_timeout_seconds=0.5,
            deadline_completion_reserve_seconds=2,
            shutdown_grace_seconds=5,
            enable_openapi=False,
            log_level="warning",
        )

        def runtime_backend_factory(
            bound_lease: GenerationRequestLease,
            request_deadline: RequestDeadline,
        ) -> Any:
            if bound_lease != base_lease:
                raise RuntimeReleaseError("preserved three-law scenario-eval lease mismatch")
            selected_attestor(request_deadline)
            current = selected_observation_loader(request_deadline)
            _validate_exact_observation(
                current,
                expected_loaded_law_count=expected_loaded_law_count,
            )
            if current != initial_observation:
                raise RuntimeReleaseError("preserved three-law Data observation changed")
            return selected_database_backend_factory(current, request_deadline)

        trace_writer = SafeScenarioEvalTraceWriter(trace_destination)
        closeable_list: list[Any] = [trace_writer]
        if readonly_boundary is not None:
            closeable_list.append(readonly_boundary)
        if created_neo4j_executor and shared_neo4j_executor is not None:
            closeable_list.append(shared_neo4j_executor)
        application = create_test_runtime_v3_application(
            service_settings=service_settings,
            runtime_release_loader=runtime_loader,
            backend_factory=runtime_backend_factory,
            runtime_closeables=tuple(closeable_list),
            model_supervisor=(
                shared_model_supervisor if database_backend_factory is None else None
            ),
        )
        manifest = ThreeLawV3ScenarioEvalManifest(
            data_revision=provider_lock.provider_revision,
            observation=initial_observation,
            identity=runtime_lease.identity,
            policy_binding=binding,
        )
        write_test_only_v3_identity_json(
            runtime_lease.identity,
            output=identity_destination,
            provider_context=context,
        )
        wrote_identity = True
        v2.write_diagnostic_manifest(
            manifest,
            manifest_destination,
            forbidden_roots=(ROOT, context),
        )
        wrote_manifest = True
        application.add_middleware(
            ScenarioEvalTraceMiddleware,
            collector=trace_collector,
            writer=trace_writer,
            identity=_trace_identity(runtime_lease.identity),
            knowledge_snapshot_id=initial_observation.knowledge_snapshot_id,
        )
    except Exception as exc:
        if wrote_identity:
            identity_destination.unlink(missing_ok=True)
        if wrote_manifest:
            manifest_destination.unlink(missing_ok=True)
        if trace_writer is not None:
            trace_writer.close()
            trace_destination.unlink(missing_ok=True)
        if readonly_boundary is not None:
            readonly_boundary.close()
        if created_neo4j_executor and shared_neo4j_executor is not None:
            close_neo4j = getattr(shared_neo4j_executor, "close", None)
            if not callable(close_neo4j):
                raise ThreeLawV3ScenarioEvalError(
                    "owned Neo4j executor has no close operation"
                ) from exc
            close_neo4j()
        if created_model_supervisor and shared_model_supervisor is not None:
            shared_model_supervisor.close()
        raise

    application.state.test_only_runtime_conformance = False
    application.state.test_only_three_law_v3_scenario_eval = True
    application.state.evidence_profile = EVIDENCE_PROFILE
    application.state.evidence_scope = EVIDENCE_SCOPE
    application.state.evidence_data_source = EVIDENCE_DATA_SOURCE
    application.state.evidence_policy_mode = SCENARIO_EVAL_POLICY_MODE
    application.state.three_law_v3_scenario_eval_manifest = manifest.to_wire()
    application.state.scenario_eval_trace_output = trace_destination
    application.state.rag_quality_evaluated = False
    application.state.domain_reviewed = False
    application.state.release_ready = False
    application.state.automatic_completion_eligible = False

    @application.get(SCENARIO_EVAL_READINESS_PATH, include_in_schema=False)
    def scenario_eval_readiness(request: Request) -> dict[str, object]:
        readiness = runtime_v3_readiness(request)
        return {
            "status": "ready",
            "mode": "PRESERVED_THREE_LAW_V3_SCENARIO_EVAL",
            "evidence_profile": EVIDENCE_PROFILE,
            "evidence_scope": EVIDENCE_SCOPE,
            "evidence_data_source": EVIDENCE_DATA_SOURCE,
            "evidence_policy_mode": SCENARIO_EVAL_POLICY_MODE,
            "expected_production_law_count": EXPECTED_PRODUCTION_LAW_COUNT,
            "observed_law_count": initial_observation.total_law_count,
            "observed_article_count": initial_observation.article_count,
            "qdrant_point_count": initial_observation.qdrant_point_count,
            "neo4j_article_count": initial_observation.neo4j_article_count,
            "knowledge_snapshot_id": initial_observation.knowledge_snapshot_id,
            "ccct_preflight": {
                "hsk10": SCENARIO_EVAL_HSK10,
                "required_document_code": SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
                "present": True,
            },
            "test_policy_fixture_approved": True,
            "verification_policy": "human_only",
            "legal_approval": False,
            "release_ready": False,
            "rag_quality_evaluated": False,
            "domain_reviewed": False,
            "automatic_completion_eligible": False,
            "reranker": {
                "model_id": RERANK_MODEL.model_id,
                "revision": RERANK_MODEL.revision,
                "ready": True,
                "mode": "required-fail-closed",
            },
            # `runtime_v3_readiness()` immediately above re-observes all three
            # stores and rejects any difference from this sealed observation.
            "store_state": _store_state_projection(initial_observation),
            "trace_schema_version": _TRACE_SCHEMA_VERSION,
            "evidence_runtime_release": readiness.evidence_runtime_release.model_dump(mode="json"),
        }

    return application


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--scenario-eval-only-three-law",
        action="store_true",
        required=True,
        help="mandatory acknowledgement that this is a non-release 3/58 UAT evaluation",
    )
    parser.add_argument("--provider-context", type=Path, required=True)
    parser.add_argument("--service-token-file", type=Path, required=True)
    parser.add_argument("--identity-output", type=Path, required=True)
    parser.add_argument("--scenario-manifest-output", type=Path, required=True)
    parser.add_argument("--trace-output", type=Path, required=True)
    parser.add_argument("--host", choices=LITERAL_LOOPBACK_HOSTS, required=True)
    parser.add_argument("--port", type=_port, required=True)
    parser.add_argument(
        "--expected-loaded-law-count",
        type=int,
        choices=(EXPECTED_SCENARIO_LAW_COUNT,),
        default=EXPECTED_SCENARIO_LAW_COUNT,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        application = create_three_law_v3_scenario_eval_application(
            scenario_eval_only_ack=args.scenario_eval_only_three_law,
            provider_context=args.provider_context,
            service_token_file=args.service_token_file,
            identity_output=args.identity_output,
            scenario_manifest_output=args.scenario_manifest_output,
            trace_output=args.trace_output,
            host=args.host,
            port=args.port,
            expected_loaded_law_count=args.expected_loaded_law_count,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "preserved_three_law_v3_scenario_eval_configuration_failed",
                    "exception_type": type(exc).__name__,
                    "reason": "invalid_configuration",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78

    manifest = application.state.three_law_v3_scenario_eval_manifest
    print(
        json.dumps(
            {
                "event": "preserved_three_law_v3_scenario_eval_starting",
                "evidence_profile": EVIDENCE_PROFILE,
                "evidence_scope": EVIDENCE_SCOPE,
                "evidence_data_source": EVIDENCE_DATA_SOURCE,
                "evidence_policy_mode": SCENARIO_EVAL_POLICY_MODE,
                "host": args.host,
                "port": args.port,
                "observed_law_count": manifest["observed_total_law_count"],
                "observed_article_count": manifest["observed_article_count"],
                "release_ready": False,
                "rag_quality_evaluated": False,
                "automatic_completion_eligible": False,
            },
            sort_keys=True,
        )
    )

    import uvicorn

    try:
        uvicorn.run(
            application,
            host=args.host,
            port=args.port,
            log_level="warning",
            access_log=False,
            proxy_headers=False,
            server_header=False,
            date_header=False,
            workers=1,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "event": "preserved_three_law_v3_scenario_eval_runtime_failed",
                    "exception_type": type(exc).__name__,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
