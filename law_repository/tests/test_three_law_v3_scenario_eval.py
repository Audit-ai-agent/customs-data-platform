from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.evidence import RequirementLookup
from src.evidence_v3 import MappingRequirementPolicyResolver
from src.runtime_api_v3 import RUNTIME_V3_RETRIEVE_PATH
from tools import run_three_law_v2_diagnostic as v2
from tools import run_three_law_v3_scenario_eval as runner
from tools.run_evidence_v2_conformance_server import VerifiedProviderRuntimeLock
from tools.scenario_eval_v3_policy import (
    SCENARIO_EVAL_POLICY_APPROVED_AT,
    SCENARIO_EVAL_POLICY_MODE,
    SCENARIO_EVAL_POLICY_RELEASE,
    SCENARIO_EVAL_POLICY_VALID_UNTIL,
    SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
    SCENARIO_EVAL_TRUST_RELEASE,
    build_scenario_eval_policy_fixture,
)
from tools.test_only_v3_policy import build_test_only_policy_fixture

_TOKEN = "preserved-three-law-scenario-eval-token-" + ("A" * 40)
_REVISION = "a" * 40
_SNAPSHOT = "ccct-preserved-three-law-scenario-eval"
_AS_OF = date(2026, 7, 10)


def _provider_lock() -> VerifiedProviderRuntimeLock:
    return VerifiedProviderRuntimeLock(
        contract="evidence-runtime-release-provider-lock",
        schema_version="1.0",
        provider_release="evidence-api-v1.2.0",
        provider_revision=_REVISION,
        provider_build_sha256="1" * 64,
        retrieval_schema_sha256="2" * 64,
        readiness_schema_sha256="3" * 64,
        provider_source_manifest_sha256="4" * 64,
    )


def _digest(label: str, value: object) -> str:
    return hashlib.sha256(f"{label}:{value}".encode()).hexdigest()


def _observation(
    *,
    article_count: int = runner.EXPECTED_SCENARIO_ARTICLE_COUNT,
    version: str = "v1",
) -> v2.ThreeLawObservation:
    qdrant_ids = _digest("qdrant", article_count)
    graph_ids = _digest("graph", article_count)
    return v2.ThreeLawObservation(
        law_ids=tuple(sorted(v2.DIAGNOSTIC_LAW_IDS)),
        total_law_count=3,
        article_count=article_count,
        qdrant_point_count=article_count,
        neo4j_article_count=article_count,
        knowledge_snapshot_id=_SNAPSHOT,
        supported_review_as_of=_AS_OF,
        law_article_manifest_sha256=_digest("articles", version),
        postgres_qdrant_ids_sha256=qdrant_ids,
        qdrant_point_ids_sha256=qdrant_ids,
        postgres_graph_ids_sha256=graph_ids,
        neo4j_article_ids_sha256=graph_ids,
        postgres_retrieval_state_sha256=_digest("postgres", version),
        qdrant_retrieval_state_sha256=_digest("qdrant-state", version),
        neo4j_retrieval_state_sha256=_digest("neo4j-state", version),
    )


def _context(root: Path) -> Path:
    context = root / "provider-context"
    for name in (
        "rag_retrieval_v3.schema.json",
        "readiness_v3.schema.json",
        "requirement_policy_v1.schema.json",
    ):
        destination = context / "contracts" / "evidence" / "v3" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((runner.ROOT / "contracts" / "evidence" / "v3" / name).read_bytes())
    return context


def _token_file(root: Path) -> Path:
    path = root / "evidence-token"
    path.write_text(_TOKEN + "\n", encoding="ascii")
    return path


class _ReadyBackend(TestOnlyConformanceEvidenceBackend):
    def __init__(
        self,
        observation: v2.ThreeLawObservation,
        *,
        document_code: str = SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
    ) -> None:
        super().__init__(effective_date=observation.supported_review_as_of)
        self.observation = observation
        self.document_code = document_code

    def validate_ready(self) -> None:
        return None

    def current_snapshot_id(self) -> str:
        self.retrieve_calls += 1
        return self.observation.knowledge_snapshot_id

    def snapshot_effective_date(self, snapshot_id: str) -> date:
        assert snapshot_id == self.observation.knowledge_snapshot_id
        return self.observation.supported_review_as_of

    def lookup_requirements(
        self,
        snapshot_id: str,
        hsk10: str,
        trade_type: str,
        as_of: date,
    ) -> RequirementLookup:
        assert snapshot_id == self.observation.knowledge_snapshot_id
        original = super().lookup_requirements(
            "test-only-conformance-snapshot",
            hsk10,
            trade_type,
            as_of,
        )
        return RequirementLookup(
            original.covered,
            tuple({**row, "document_code": self.document_code} for row in original.rows),
        )


def _body() -> dict[str, Any]:
    return {
        "schema_version": "3.0",
        "request_id": "req-three-law-scenario-eval",
        "case_id": "case-three-law-scenario-eval",
        "review_run_id": "run-three-law-scenario-eval",
        "review_as_of": _AS_OF.isoformat(),
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 5,
        "items": [
            {
                "item_id": "invoice:1",
                "description": "SECRET-DOCUMENT-CONTENT frozen mackerel",
                "hs_code": runner.SCENARIO_EVAL_HSK10,
                "origin_country": "NO",
                "destination_country": "KR",
            }
        ],
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
        "query_intent": "law_support DO-NOT-LOG-QUERY",
    }


def _application(
    root: Path,
    *,
    observation_loader,
    document_code: str = SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
):
    root.mkdir()
    outputs = root / "outputs"
    outputs.mkdir()
    return runner.create_three_law_v3_scenario_eval_application(
        scenario_eval_only_ack=True,
        provider_context=_context(root),
        service_token_file=_token_file(root),
        identity_output=outputs / "identity.json",
        scenario_manifest_output=outputs / "manifest.json",
        trace_output=outputs / "trace.jsonl",
        host="127.0.0.1",
        port=18014,
        observation_loader=observation_loader,
        provider_lock_loader=lambda _context: _provider_lock(),
        database_backend_factory=lambda observation, _deadline: _ReadyBackend(
            observation,
            document_code=document_code,
        ),
        executing_repository_attestor=lambda _deadline=None: None,
        install_readonly_boundary=False,
    )


def test_parser_requires_scenario_ack_and_literal_loopback(tmp_path: Path) -> None:
    common = [
        "--provider-context",
        str(tmp_path / "context"),
        "--service-token-file",
        str(tmp_path / "token"),
        "--identity-output",
        str(tmp_path / "identity.json"),
        "--scenario-manifest-output",
        str(tmp_path / "manifest.json"),
        "--trace-output",
        str(tmp_path / "trace.jsonl"),
        "--host",
        "127.0.0.1",
        "--port",
        "18014",
    ]
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(common)
    for forbidden_host in ("0.0.0.0", "localhost"):
        with pytest.raises(SystemExit):
            runner.build_parser().parse_args(
                [
                    "--scenario-eval-only-three-law",
                    *common[:-4],
                    "--host",
                    forbidden_host,
                    "--port",
                    "18014",
                ]
            )
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            [
                "--scenario-eval-only-three-law",
                *common,
                "--expected-loaded-law-count",
                "58",
            ]
        )


def test_startup_requires_exact_counts_and_actual_ccct_document(tmp_path: Path) -> None:
    with pytest.raises(runner.ThreeLawV3ScenarioEvalError, match="1,007"):
        _application(
            tmp_path / "wrong-count",
            observation_loader=lambda _deadline=None: _observation(article_count=1_008),
        )
    with pytest.raises(runner.ThreeLawV3ScenarioEvalError, match="required document"):
        _application(
            tmp_path / "wrong-code",
            observation_loader=lambda _deadline=None: _observation(),
            document_code="CCCT_DOC_ANOTHER_CODE",
        )


def test_startup_rejects_store_drift_across_ccct_preflight(tmp_path: Path) -> None:
    observations = iter(
        (
            _observation(),
            replace(
                _observation(),
                postgres_retrieval_state_sha256=_digest("postgres", "changed"),
            ),
        )
    )
    with pytest.raises(runner.ThreeLawV3ScenarioEvalError, match="changed"):
        _application(
            tmp_path / "drift",
            observation_loader=lambda _deadline=None: next(observations),
        )


def test_manifest_and_readiness_expose_exact_profile_policy_and_identity(
    tmp_path: Path,
) -> None:
    observation = _observation()
    application = _application(
        tmp_path / "ready",
        observation_loader=lambda _deadline=None: observation,
    )
    identity = application.state.runtime_v3_release_loader.acquire().identity
    output = tmp_path / "ready" / "outputs"
    identity_file = json.loads((output / "identity.json").read_text(encoding="utf-8"))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

    assert len(identity.binding_headers()) == 17
    assert identity_file == identity.model_dump(mode="json")
    assert manifest["schema_version"] == "three-law-evidence-v3-scenario-eval/v1"
    assert manifest["evidence_profile"] == "preserved-3law-v3-scenario-eval"
    assert manifest["evidence_scope"] == "ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58"
    assert manifest["evidence_data_source"] == runner.EVIDENCE_DATA_SOURCE
    assert manifest["evidence_policy_mode"] == SCENARIO_EVAL_POLICY_MODE
    assert manifest["observed_total_law_count"] == 3
    assert manifest["observed_article_count"] == 1_007
    assert manifest["qdrant_point_count"] == 1_007
    assert manifest["neo4j_article_count"] == 1_007
    assert manifest["ccct_preflight"]["required_document_code"] == (
        SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE
    )
    assert manifest["test_policy"] == {
        "approved_fixture_signature": True,
        "legal_approval": False,
        "production_authority": False,
        "verification_policy": "human_only",
    }
    assert manifest["test_policy_binding"] == {
        "required_document_code": SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
        "verification_policy": "human_only",
        "policy_source_sha256": hashlib.sha256(
            b"three-law-scenario-eval/human-only-policy/NON-PRODUCTION"
        ).hexdigest(),
        "policy_release": SCENARIO_EVAL_POLICY_RELEASE,
        "approved": True,
    }
    assert manifest["release_ready"] is False
    assert manifest["domain_reviewed"] is False
    assert manifest["automatic_completion_eligible"] is False
    assert manifest["retrieval_runtime"] == {
        "postgresql": "structured-ledger-read-only",
        "qdrant": ["bge-m3-dense", "bm25-sparse", "rrf"],
        "reranker": {
            "model_id": "BAAI/bge-reranker-v2-m3",
            "revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
            "mode": "required-fail-closed",
        },
        "neo4j": "one-hop-law-and-requirement-expansion",
    }
    assert manifest["store_state"] == {
        "knowledge_snapshot_id": observation.knowledge_snapshot_id,
        "postgres_article_count": 1_007,
        "qdrant_point_count": 1_007,
        "neo4j_article_count": 1_007,
        "postgres_retrieval_state_sha256": observation.postgres_retrieval_state_sha256,
        "qdrant_retrieval_state_sha256": observation.qdrant_retrieval_state_sha256,
        "neo4j_retrieval_state_sha256": observation.neo4j_retrieval_state_sha256,
        "combined_state_sha256": runner._store_state_projection(observation)[
            "combined_state_sha256"
        ],
    }
    assert identity.requirement_policy_release == SCENARIO_EVAL_POLICY_RELEASE

    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51020),
    ) as client:
        response = client.get(
            runner.SCENARIO_EVAL_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json()["store_state"] == manifest["store_state"]
    payload = response.json()
    assert payload["evidence_profile"] == runner.EVIDENCE_PROFILE
    assert payload["evidence_scope"] == runner.EVIDENCE_SCOPE
    assert payload["evidence_data_source"] == runner.EVIDENCE_DATA_SOURCE
    assert payload["evidence_policy_mode"] == SCENARIO_EVAL_POLICY_MODE
    assert payload["test_policy_fixture_approved"] is True
    assert payload["verification_policy"] == "human_only"
    assert payload["legal_approval"] is False
    assert payload["release_ready"] is False
    assert payload["rag_quality_evaluated"] is False
    assert payload["automatic_completion_eligible"] is False
    assert payload["reranker"] == {
        "model_id": "BAAI/bge-reranker-v2-m3",
        "revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        "ready": True,
        "mode": "required-fail-closed",
    }
    assert payload["evidence_runtime_release"] == identity_file


def test_actual_code_is_approved_human_only_and_trace_is_safe(tmp_path: Path) -> None:
    application = _application(
        tmp_path / "retrieve",
        observation_loader=lambda _deadline=None: _observation(),
    )
    identity = application.state.runtime_v3_release_loader.acquire().identity
    body = _body()
    headers = {
        "Authorization": f"Bearer {_TOKEN}",
        "Content-Type": "application/json",
        "X-Request-ID": body["request_id"],
        **identity.binding_headers(),
    }

    with TestClient(
        application,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51021),
    ) as client:
        retrieved = client.post(RUNTIME_V3_RETRIEVE_PATH, json=body, headers=headers)

    assert retrieved.status_code == 200
    payload = retrieved.json()
    assert payload["status"] == "ok"
    assert payload["results"][0]["coverage"] == "complete"
    requirement = next(
        row
        for row in payload["results"][0]["evidence"]
        if row["required_document_code"] == SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE
    )
    assert requirement["requirement_verification"] == {
        "required_document_code": SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
        "verification_policy": "human_only",
        "policy_source_sha256": requirement["requirement_verification"]["policy_source_sha256"],
        "policy_release": SCENARIO_EVAL_POLICY_RELEASE,
        "approved": True,
    }

    trace_path = tmp_path / "retrieve" / "outputs" / "trace.jsonl"
    raw_trace = trace_path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in raw_trace.splitlines()]
    assert len(rows) == 1
    trace = rows[0]
    assert trace["request_id"] == body["request_id"]
    assert trace["http_status_code"] == 200
    assert trace["evidence_profile"] == runner.EVIDENCE_PROFILE
    assert trace["evidence_policy_mode"] == SCENARIO_EVAL_POLICY_MODE
    assert trace["knowledge_snapshot_id"] == _SNAPSHOT
    assert set(trace["retrieval"]) == {
        "structured",
        "dense",
        "bm25",
        "rerank",
        "graph",
    }
    assert all(trace["retrieval"][stage]["attempted"] for stage in trace["retrieval"])
    assert all(trace["retrieval"][stage]["succeeded"] for stage in trace["retrieval"])
    assert trace["retrieval"]["structured"]["result_count"] == 1
    base_identity = identity.base_runtime_release
    assert trace["provider_identity"] == {
        "provider_release": base_identity.provider_release,
        "provider_revision": base_identity.provider_revision,
        "provider_build_sha256": base_identity.provider_build_sha256,
        "knowledge_generation_id": base_identity.knowledge_generation_id,
        "knowledge_generation_attestation_sha256": (
            base_identity.knowledge_generation_attestation_sha256
        ),
        "knowledge_source_manifest_sha256": base_identity.knowledge_source_manifest_sha256,
        "supported_review_as_of": base_identity.supported_review_as_of.isoformat(),
        "runtime_release_sha256": base_identity.runtime_release_sha256,
        "runtime_release_v3_sha256": identity.runtime_release_v3_sha256,
        "requirement_policy_release": identity.requirement_policy_release,
        "requirement_policy_manifest_sha256": identity.requirement_policy_manifest_sha256,
        "requirement_policy_approval_sha256": identity.requirement_policy_approval_sha256,
        "reviewer_trust_store_sha256": identity.reviewer_trust_store_sha256,
    }
    for forbidden in (
        "SECRET-DOCUMENT-CONTENT",
        "DO-NOT-LOG-QUERY",
        _TOKEN,
        runner.SCENARIO_EVAL_HSK10,
    ):
        assert forbidden not in raw_trace


def test_required_reranker_reorders_rrf_candidates_and_scores_graph_results() -> None:
    rows = {
        article_pk: {
            "article_pk": article_pk,
            "law_id": "001556",
            "law_name": "관세법",
            "hierarchy": "법률",
            "article_no": article_no,
            "paragraph_no": None,
            "item_no": None,
            "title": title,
            "content": content,
            "content_hash": f"{article_pk:064x}",
            "version_mst": "mst-1",
            "valid_from": _AS_OF,
            "valid_to": None,
        }
        for article_pk, article_no, title, content in (
            (1, 4400, "연관성 낮음", "unrelated article"),
            (2, 24500, "신고 시의 제출서류", "relevant submission documents"),
            (3, 24900, "신고사항의 보완", "relevant declaration correction"),
        )
    }

    class SearchBackend(runner.ScenarioEvalDatabaseBackend):
        def __init__(self) -> None:
            self._scenario_trace_collector = runner.ScenarioEvalTraceCollector()
            self._scenario_reranker_readiness = lambda: True

            def score(_query: str, passages: list[str]) -> list[float]:
                return [
                    0.95
                    if "submission" in passage
                    else 0.8
                    if "correction" in passage
                    else 0.1
                    for passage in passages
                ]

            self._scenario_rerank_scorer = score

        def _require_deadline(self):
            return None

        def _arm_hits(self, _query, arm, _recall, _as_of):
            if arm == runner.DENSE_VECTOR:
                return [
                    SimpleNamespace(article_pk=1, score=0.9, text=rows[1]["content"]),
                    SimpleNamespace(article_pk=2, score=0.8, text=rows[2]["content"]),
                ]
            return [
                SimpleNamespace(article_pk=2, score=8.0, text=rows[2]["content"]),
                SimpleNamespace(article_pk=1, score=7.0, text=rows[1]["content"]),
            ]

        def _run_neo4j(self, _query, parameters):
            return [{"article_pk": 3}] if parameters["pk"] == 2 else []

        def _law_rows(self, article_pks, _as_of):
            return {article_pk: rows[article_pk] for article_pk in article_pks}

    matches = SearchBackend().search_law_articles("submission correction", 3, _AS_OF)

    assert [match.row["article_pk"] for match in matches] == [2, 3, 1]
    assert matches[0].raw_scores["reranker"] == 0.95
    assert matches[1].retrieval_methods == ("graph",)
    assert matches[1].raw_scores["reranker"] == 0.8
    assert all(0.0 <= match.score <= 1.0 for match in matches)


def test_scenario_eval_reranker_failure_is_not_lexically_degraded() -> None:
    with pytest.raises(runner.ThreeLawV3ScenarioEvalError, match="reranking failed"):
        runner._validated_rerank_scores(
            lambda _query, _passages: (_ for _ in ()).throw(RuntimeError("model unavailable")),
            "safe query",
            ["safe passage"],
        )

    with pytest.raises(runner.ThreeLawV3ScenarioEvalError, match="reranker is unavailable"):
        runner._require_reranker_ready(lambda: False)


def test_policy_is_exact_code_only_and_distinct_from_existing_diagnostic() -> None:
    scenario = build_scenario_eval_policy_fixture(
        knowledge_source_manifest_sha256="a" * 64,
        knowledge_snapshot_id=_SNAPSHOT,
        today=SCENARIO_EVAL_POLICY_APPROVED_AT,
    )
    diagnostic = build_test_only_policy_fixture(
        knowledge_source_manifest_sha256="a" * 64,
        knowledge_snapshot_id=_SNAPSHOT,
        required_document_code="TEST_ONLY_PRESERVED_3LAW_NEVER_MATCH",
        today=_AS_OF,
    )
    bindings = scenario.verified.bindings()
    assert set(bindings) == {SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE}
    assert bindings[SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE].approved is True
    assert bindings[SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE].verification_policy == "human_only"
    unknown = MappingRequirementPolicyResolver(bindings).resolve("CCCT_DOC_UNKNOWN")
    assert unknown.approved is False
    assert unknown.verification_policy == "human_only"
    assert scenario.verified.release.policy_release != diagnostic.verified.release.policy_release
    assert SCENARIO_EVAL_TRUST_RELEASE != diagnostic.trust_store.trust_store_release
    assert scenario.verified.reviewer_trust_store_sha256 != (
        diagnostic.verified.reviewer_trust_store_sha256
    )


def test_sealed_policy_identity_is_stable_within_window_and_expires() -> None:
    approved_day = build_scenario_eval_policy_fixture(
        knowledge_source_manifest_sha256="a" * 64,
        knowledge_snapshot_id=_SNAPSHOT,
        today=SCENARIO_EVAL_POLICY_APPROVED_AT,
    )
    last_valid_day = build_scenario_eval_policy_fixture(
        knowledge_source_manifest_sha256="a" * 64,
        knowledge_snapshot_id=_SNAPSHOT,
        today=SCENARIO_EVAL_POLICY_VALID_UNTIL,
    )

    assert approved_day.verified.release == last_valid_day.verified.release
    assert approved_day.trust_store == last_valid_day.trust_store
    assert approved_day.trust_store_lock == last_valid_day.trust_store_lock
    assert approved_day.verified.release.policy_manifest_sha256 == (
        last_valid_day.verified.release.policy_manifest_sha256
    )
    assert approved_day.verified.release.approval_sha256 == (
        last_valid_day.verified.release.approval_sha256
    )
    assert approved_day.verified.reviewer_trust_store_sha256 == (
        last_valid_day.verified.reviewer_trust_store_sha256
    )

    with pytest.raises(ValueError, match="not active yet"):
        build_scenario_eval_policy_fixture(
            knowledge_source_manifest_sha256="a" * 64,
            knowledge_snapshot_id=_SNAPSHOT,
            today=SCENARIO_EVAL_POLICY_APPROVED_AT - timedelta(days=1),
        )
    with pytest.raises(ValueError, match="expired"):
        build_scenario_eval_policy_fixture(
            knowledge_source_manifest_sha256="a" * 64,
            knowledge_snapshot_id=_SNAPSHOT,
            today=SCENARIO_EVAL_POLICY_VALID_UNTIL + timedelta(days=1),
        )


def test_repository_attestation_binds_runner_and_policy_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_calls: list[VerifiedProviderRuntimeLock] = []
    seen: list[str] = []
    monkeypatch.setattr(
        v2,
        "attest_executing_repository",
        lambda lock, **_kwargs: base_calls.append(lock),
    )

    def committed(_repository: Path, *_arguments: str, **_kwargs: Any) -> bytes:
        revision_path = _arguments[-1].split(":", 1)[1]
        seen.append(revision_path)
        return (runner.ROOT / revision_path).read_bytes()

    monkeypatch.setattr(v2, "_git", committed)
    runner.attest_executing_scenario_eval_repository(_provider_lock())
    assert base_calls == [_provider_lock()]
    assert seen == [
        "tools/run_three_law_v3_scenario_eval.py",
        "tools/scenario_eval_v3_policy.py",
    ]


def test_all_outputs_must_remain_outside_source_and_provider_context(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    with pytest.raises(runner.ThreeLawV3ScenarioEvalError, match="outside"):
        runner._validate_new_output_path(
            runner.ROOT / "forbidden-trace.jsonl",
            context=context,
            label="trace",
        )
    with pytest.raises(runner.ThreeLawV3ScenarioEvalError, match="outside"):
        runner._validate_new_output_path(
            context / "forbidden-trace.jsonl",
            context=context,
            label="trace",
        )
