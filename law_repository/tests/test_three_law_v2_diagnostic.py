from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.conformance import TestOnlyConformanceEvidenceBackend
from src.deadline import DeadlineExceeded, RequestDeadline
from src.runtime_api import RUNTIME_READINESS_PATH, RUNTIME_RETRIEVE_PATH
from tools import run_three_law_v2_diagnostic as runner
from tools.run_evidence_v2_conformance_server import VerifiedProviderRuntimeLock

_TOKEN = "three-law-diagnostic-token-" + ("A" * 40)
_REVISION = "a" * 40
_SNAPSHOT = "ccct-diagnostic-snapshot"
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
    article_count: int = 1_007,
    content_version: str = "v1",
) -> runner.ThreeLawObservation:
    qdrant_ids_digest = _digest("qdrant", article_count)
    graph_ids_digest = _digest("graph", article_count)
    return runner.ThreeLawObservation(
        law_ids=tuple(sorted(runner.DIAGNOSTIC_LAW_IDS)),
        total_law_count=3,
        article_count=article_count,
        qdrant_point_count=article_count,
        neo4j_article_count=article_count,
        knowledge_snapshot_id=_SNAPSHOT,
        supported_review_as_of=_AS_OF,
        law_article_manifest_sha256=_digest("law-articles", content_version),
        postgres_qdrant_ids_sha256=qdrant_ids_digest,
        qdrant_point_ids_sha256=qdrant_ids_digest,
        postgres_graph_ids_sha256=graph_ids_digest,
        neo4j_article_ids_sha256=graph_ids_digest,
        postgres_retrieval_state_sha256=_digest("postgres-state", content_version),
        qdrant_retrieval_state_sha256=_digest("qdrant-state", content_version),
        neo4j_retrieval_state_sha256=_digest("neo4j-state", content_version),
    )


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "service-token"
    path.write_text(_TOKEN + "\n", encoding="ascii")
    return path


class _ReadyBackend(TestOnlyConformanceEvidenceBackend):
    def __init__(self, observation: runner.ThreeLawObservation) -> None:
        super().__init__(effective_date=observation.supported_review_as_of)
        self.observation = observation
        self.readiness_calls = 0

    def validate_ready(self) -> None:
        self.readiness_calls += 1

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
    ):
        assert snapshot_id == self.observation.knowledge_snapshot_id
        return super().lookup_requirements(
            "test-only-conformance-snapshot",
            hsk10,
            trade_type,
            as_of,
        )


def _body() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": "req-three-law-diagnostic",
        "case_id": "case-three-law-diagnostic",
        "review_run_id": "run-three-law-diagnostic",
        "review_as_of": _AS_OF.isoformat(),
        "jurisdiction": "KR",
        "trade_type": "import",
        "top_k": 5,
        "items": [
            {
                "item_id": "invoice:1",
                "description": "Frozen mackerel",
                "hs_code": "0303540000",
                "origin_country": "NO",
                "destination_country": "KR",
            }
        ],
        "uploaded_requirement_document_codes": [],
        "validation_issue_codes": [],
        "query_intent": "required_import_documents",
    }


def _app(
    tmp_path: Path,
    *,
    observation_loader,
    output_name: str = "three-law-manifest.json",
):
    context = tmp_path / "provider-context"
    context.mkdir(exist_ok=True)
    output_root = tmp_path / "artifacts"
    output_root.mkdir(exist_ok=True)
    return runner.create_three_law_diagnostic_application(
        diagnostic_only_ack=True,
        provider_context=context,
        service_token_file=_token_file(tmp_path),
        diagnostic_manifest_output=output_root / output_name,
        host="127.0.0.1",
        port=18012,
        observation_loader=observation_loader,
        provider_lock_loader=lambda _context: _provider_lock(),
        database_backend_factory=lambda observation, _deadline: _ReadyBackend(observation),
        executing_repository_attestor=lambda _deadline=None: None,
        install_readonly_boundary=False,
    )


def test_parser_requires_explicit_diagnostic_flag_and_loopback(tmp_path: Path) -> None:
    common = [
        "--provider-context",
        str(tmp_path),
        "--service-token-file",
        str(tmp_path / "token"),
        "--diagnostic-manifest-output",
        str(tmp_path / "manifest.json"),
        "--host",
        "127.0.0.1",
        "--port",
        "18012",
    ]
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(common)
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            [
                "--diagnostic-only-three-law",
                *common[:-4],
                "--host",
                "0.0.0.0",
                "--port",
                "18012",
            ]
        )
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            [
                "--diagnostic-only-three-law",
                *common,
                "--expected-loaded-law-count",
                "58",
            ]
        )


def test_domain_separated_identity_binds_actual_snapshot_date_and_counts() -> None:
    first = runner.derive_three_law_runtime_lease(_provider_lock(), _observation())
    changed = runner.derive_three_law_runtime_lease(
        _provider_lock(),
        _observation(article_count=1_008),
    )

    assert first.identity.knowledge_snapshot_id == _SNAPSHOT
    assert first.identity.supported_review_as_of == _AS_OF
    assert first.identity.evaluation_report_sha256 != "0" * 64
    assert first.identity.runtime_release_sha256 != changed.identity.runtime_release_sha256
    assert first.identity.knowledge_generation_id != changed.identity.knowledge_generation_id


def test_domain_separated_identity_changes_when_content_changes_at_same_count() -> None:
    first = runner.derive_three_law_runtime_lease(
        _provider_lock(),
        _observation(content_version="v1"),
    )
    changed = runner.derive_three_law_runtime_lease(
        _provider_lock(),
        _observation(content_version="v2"),
    )

    assert first.identity.runtime_release_sha256 != changed.identity.runtime_release_sha256
    assert first.identity.knowledge_generation_id != changed.identity.knowledge_generation_id


@pytest.mark.parametrize(
    "changed",
    [
        {"payload": {"article_pk": 7, "text": "changed"}},
        {"vector": {"dense": [0.1, 0.200001], "bm25": {"indices": [1], "values": [1.0]}}},
        {"vector": {"dense": [0.1, 0.2], "bm25": {"indices": [2], "values": [1.0]}}},
        {"vector": {"dense": [0.1, 0.2], "bm25": {"indices": [1], "values": [0.9]}}},
    ],
)
def test_qdrant_payload_dense_and_sparse_state_mutations_change_identity(
    changed: dict[str, Any],
) -> None:
    baseline_state = {
        "collection": {"vectors": {"dense": {"size": 2}}, "sparse_vectors": {"bm25": {}}},
        "points": [
            {
                "id": "point-1",
                "payload": {"article_pk": 7, "text": "original"},
                "vector": {
                    "dense": [0.1, 0.2],
                    "bm25": {"indices": [1], "values": [1.0]},
                },
            }
        ],
    }
    mutated_state = json.loads(json.dumps(baseline_state))
    mutated_state["points"][0].update(changed)
    first_digest = runner._state_sha256(b"qdrant-retrieval", baseline_state)
    changed_digest = runner._state_sha256(b"qdrant-retrieval", mutated_state)
    assert first_digest != changed_digest

    first = runner.derive_three_law_runtime_lease(
        _provider_lock(),
        replace(_observation(), qdrant_retrieval_state_sha256=first_digest),
    )
    second = runner.derive_three_law_runtime_lease(
        _provider_lock(),
        replace(_observation(), qdrant_retrieval_state_sha256=changed_digest),
    )
    assert first.identity.runtime_release_sha256 != second.identity.runtime_release_sha256


@pytest.mark.parametrize(
    ("label", "baseline", "changed"),
    [
        (
            b"postgres-retrieval",
            {
                "ccct_requirements": [{"version_hash": "a", "source_uri": "https://a"}],
                "law_articles": [{"id": 1, "content": "old", "valid_to": "2026-01-01"}],
            },
            {
                "ccct_requirements": [{"version_hash": "b", "source_uri": "https://a"}],
                "law_articles": [{"id": 1, "content": "old", "valid_to": "2026-01-01"}],
            },
        ),
        (
            b"postgres-retrieval",
            {
                "ccct_queries": [{"hsk10": "0303540000", "covered": True}],
                "ccct_source_records": [{"source_uri": "https://source-a"}],
            },
            {
                "ccct_queries": [],
                "ccct_source_records": [{"source_uri": "https://source-b"}],
            },
        ),
        (
            b"postgres-retrieval",
            {"law_versions": [{"mst": "1"}], "law_articles": [{"content": "history-a"}]},
            {"law_versions": [{"mst": "2"}], "law_articles": [{"content": "history-b"}]},
        ),
        (
            b"neo4j-retrieval",
            {
                "nodes": [{"labels": ["HSK"], "properties": {"code": "0303540000"}}],
                "relationships": [{"source": "a", "type": "CITES", "target": "b"}],
            },
            {
                "nodes": [{"labels": ["HSK"], "properties": {"code": "0303550000"}}],
                "relationships": [{"source": "b", "type": "CITES", "target": "a"}],
            },
        ),
    ],
)
def test_every_response_affecting_store_state_mutation_changes_digest(
    label: bytes,
    baseline: object,
    changed: object,
) -> None:
    assert runner._state_sha256(label, baseline) != runner._state_sha256(label, changed)


def test_every_eligible_leaf_requires_one_non_null_unique_qdrant_mapping() -> None:
    valid = (
        (
            7,
            "point-7",
            "001556",
            1,
            None,
            None,
            "content",
            "law",
            "법률",
            date(2026, 1, 1),
        ),
    )
    point_ids, projection = runner._eligible_law_leaf_projection(valid)
    assert point_ids == ("point-7",)
    assert projection["point-7"]["article_pk"] == 7

    with pytest.raises(runner.ThreeLawDiagnosticError, match="no Qdrant point"):
        runner._eligible_law_leaf_projection(((*valid[0][:1], None, *valid[0][2:]),))
    with pytest.raises(runner.ThreeLawDiagnosticError, match="unique"):
        runner._eligible_law_leaf_projection((valid[0], (8, *valid[0][1:])))


def test_qdrant_scroll_rejects_null_point_identity() -> None:
    class _Qdrant:
        def scroll(self, **_kwargs):
            return [SimpleNamespace(id=None, payload={}, vector={})], None

    with pytest.raises(runner.ThreeLawDiagnosticError, match="without an identity"):
        runner._observe_qdrant_state(
            1,
            collection_state={},
            eligible_by_point={},
            scroll_page=lambda _offset, _limit: _Qdrant().scroll(),
        )


def test_qdrant_observer_preserves_legacy_digest_with_bounded_pages() -> None:
    expected_payloads = {
        "point-1": {
            "article_pk": 1,
            "law_id": "001556",
            "law_name": "관세법",
            "hierarchy": "법률",
            "article_no": 1,
            "paragraph_no": None,
            "item_no": None,
            "enforcement_date": "2026-01-01",
            "text": "first",
        },
        "point-2": {
            "article_pk": 2,
            "law_id": "002421",
            "law_name": "관세법 시행령",
            "hierarchy": "대통령령",
            "article_no": 2,
            "paragraph_no": None,
            "item_no": None,
            "enforcement_date": "2026-01-01",
            "text": "second",
        },
    }
    records = [
        SimpleNamespace(
            id=point_id,
            payload=payload,
            vector={"dense": [float(index), float(index + 1)]},
        )
        for index, (point_id, payload) in enumerate(reversed(expected_payloads.items()))
    ]

    collection = {"status": "green", "vectors_count": 2}
    limits: list[int] = []

    def scroll_page(_offset, limit):
        limits.append(limit)
        return records, None

    point_ids, observed_digest = runner._observe_qdrant_state(
        2,
        collection_state=collection,
        eligible_by_point=expected_payloads,
        scroll_page=scroll_page,
    )
    legacy_points = tuple(
        sorted(
            (
                {
                    "id": str(record.id),
                    "payload": runner._canonical_value(record.payload),
                    "vector": runner._canonical_value(record.vector),
                }
                for record in records
            ),
            key=lambda item: str(item["id"]),
        )
    )

    assert point_ids == ("point-1", "point-2")
    assert observed_digest == runner._state_sha256(
        b"qdrant-retrieval",
        {"collection": collection, "points": legacy_points},
    )
    assert limits == [runner._QDRANT_SCROLL_PAGE_SIZE]
    assert runner._QDRANT_SCROLL_PAGE_SIZE <= 64


def test_each_qdrant_http_operation_owns_a_fresh_bounded_client() -> None:
    clients = []

    class _Client:
        def __init__(self) -> None:
            self.closed = False
            clients.append(self)

        def value(self, number: int) -> int:
            assert self.closed is False
            return number

        def close(self) -> None:
            self.closed = True

    deadline = RequestDeadline.start(
        started_at=runner.time.monotonic(),
        request_timeout_seconds=5,
        store_timeout_seconds=2,
        model_timeout_seconds=2,
        completion_reserve_seconds=0.5,
    )
    factory = lambda **_kwargs: _Client()

    assert runner._bounded_qdrant_call(deadline, factory, lambda client: client.value(1)) == 1
    assert runner._bounded_qdrant_call(deadline, factory, lambda client: client.value(2)) == 2
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_startup_observation_budget_preserves_per_operation_caps() -> None:
    started = runner.time.monotonic()
    deadline = runner._deadline()

    assert deadline.expires_at - started == pytest.approx(
        runner.DIAGNOSTIC_STARTUP_OBSERVATION_DEADLINE_SECONDS,
        abs=0.1,
    )
    assert deadline.store_timeout_seconds == 20
    assert deadline.model_timeout_seconds == 20
    assert deadline.completion_reserve_seconds == 2
    assert (
        runner.DIAGNOSTIC_STARTUP_OBSERVATION_DEADLINE_SECONDS
        == runner.DIAGNOSTIC_SERVICE_REQUEST_DEADLINE_SECONDS
    )


def test_executing_repository_attestation_rejects_dirty_or_wrong_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner_bytes = Path(runner.__file__).read_bytes()

    def clean_exact(_repository, *arguments, **_kwargs):
        if arguments[0] == "status":
            return ""
        if arguments[0] == "rev-parse":
            return _REVISION
        return runner_bytes

    monkeypatch.setattr(runner, "_git", clean_exact)
    runner.attest_executing_repository(_provider_lock())

    def dirty(_repository, *arguments, **_kwargs):
        return " M tools/run_three_law_v2_diagnostic.py" if arguments[0] == "status" else ""

    monkeypatch.setattr(runner, "_git", dirty)
    with pytest.raises(runner.ThreeLawDiagnosticError, match="not clean"):
        runner.attest_executing_repository(_provider_lock())

    def wrong_revision(_repository, *arguments, **_kwargs):
        if arguments[0] == "status":
            return ""
        return "b" * 40

    monkeypatch.setattr(runner, "_git", wrong_revision)
    with pytest.raises(runner.ThreeLawDiagnosticError, match="does not match"):
        runner.attest_executing_repository(_provider_lock())


def test_expired_deadline_starts_no_observation_or_attestation_work() -> None:
    calls: list[str] = []
    lease = runner.derive_three_law_runtime_lease(_provider_lock(), _observation())
    expired = RequestDeadline.start(
        started_at=0,
        request_timeout_seconds=2,
        store_timeout_seconds=1,
        model_timeout_seconds=1,
        completion_reserve_seconds=0.5,
        clock=lambda: 3,
    )
    loader = runner._ObservedDiagnosticRuntimeLeaseLoader(
        lease,
        initial_observation=_observation(),
        observation_loader=lambda _deadline=None: calls.append("observe") or _observation(),
        executing_repository_attestor=(lambda _deadline=None: calls.append("attest")),
        expected_loaded_law_count=3,
    )

    with pytest.raises(DeadlineExceeded):
        loader.assert_unchanged(lease, expired)
    assert calls == []


def test_same_request_deadline_reaches_attestor_observer_backend_and_postcheck(
    tmp_path: Path,
) -> None:
    observation = _observation()
    observed_deadlines: list[RequestDeadline | None] = []
    attested_deadlines: list[RequestDeadline | None] = []
    backend_deadlines: list[RequestDeadline] = []

    def observe(deadline: RequestDeadline | None = None) -> runner.ThreeLawObservation:
        observed_deadlines.append(deadline)
        return observation

    def attest(deadline: RequestDeadline | None = None) -> None:
        attested_deadlines.append(deadline)

    def backend_factory(
        current: runner.ThreeLawObservation,
        deadline: RequestDeadline,
    ) -> _ReadyBackend:
        assert current == observation
        backend_deadlines.append(deadline)
        return _ReadyBackend(current)

    context = tmp_path / "provider-context"
    context.mkdir()
    output_root = tmp_path / "artifacts"
    output_root.mkdir()
    app = runner.create_three_law_diagnostic_application(
        diagnostic_only_ack=True,
        provider_context=context,
        service_token_file=_token_file(tmp_path),
        diagnostic_manifest_output=output_root / "manifest.json",
        host="127.0.0.1",
        port=18012,
        observation_loader=observe,
        provider_lock_loader=lambda _context: _provider_lock(),
        database_backend_factory=backend_factory,
        executing_repository_attestor=attest,
        install_readonly_boundary=False,
    )
    assert (
        app.state.production_service.settings.request_deadline_seconds
        == runner.DIAGNOSTIC_SERVICE_REQUEST_DEADLINE_SECONDS
        == 180.0
    )
    request_deadline = RequestDeadline.start(
        started_at=0,
        request_timeout_seconds=30,
        store_timeout_seconds=10,
        model_timeout_seconds=5,
        completion_reserve_seconds=0.5,
        clock=lambda: 1,
    )
    lease = app.state.runtime_release_loader.acquire(request_deadline)
    app.state.runtime_backend_factory(lease, request_deadline)
    app.state.runtime_release_loader.assert_unchanged(lease, request_deadline)

    assert observed_deadlines[0] is not request_deadline
    assert observed_deadlines[1:] == [request_deadline, request_deadline]
    assert attested_deadlines[0] is not request_deadline
    assert attested_deadlines[1:] == [request_deadline, request_deadline]
    assert backend_deadlines == [request_deadline]


def test_authenticated_readiness_and_retrieval_use_exact_diagnostic_identity(
    tmp_path: Path,
) -> None:
    observation = _observation()
    backend_instances: list[_ReadyBackend] = []

    def backend_factory(
        current: runner.ThreeLawObservation,
        _deadline,
    ) -> _ReadyBackend:
        backend = _ReadyBackend(current)
        backend_instances.append(backend)
        return backend

    context = tmp_path / "provider-context"
    context.mkdir()
    output_root = tmp_path / "artifacts"
    output_root.mkdir()
    manifest_output = output_root / "manifest.json"
    app = runner.create_three_law_diagnostic_application(
        diagnostic_only_ack=True,
        provider_context=context,
        service_token_file=_token_file(tmp_path),
        diagnostic_manifest_output=manifest_output,
        host="127.0.0.1",
        port=18012,
        observation_loader=lambda _deadline=None: observation,
        provider_lock_loader=lambda _context: _provider_lock(),
        database_backend_factory=backend_factory,
        executing_repository_attestor=lambda _deadline=None: None,
        install_readonly_boundary=False,
    )
    identity = app.state.three_law_diagnostic_manifest["evidence_runtime_release"]
    binding_headers = app.state.runtime_release_loader.acquire().identity.binding_headers()

    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51002),
    ) as client:
        assert client.get(RUNTIME_READINESS_PATH).status_code == 401
        ready = client.get(
            RUNTIME_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        retrieved = client.post(
            RUNTIME_RETRIEVE_PATH,
            content=json.dumps(_body()).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "Content-Type": "application/json",
                "X-Request-ID": "req-three-law-diagnostic",
                **binding_headers,
            },
        )

    assert ready.status_code == 200
    assert ready.json()["evidence_runtime_release"] == identity
    assert retrieved.status_code == 200
    assert retrieved.json()["knowledge_snapshot_id"] == _SNAPSHOT
    assert backend_instances[0].readiness_calls == 1
    assert backend_instances[1].retrieve_calls == 1
    manifest = json.loads(manifest_output.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "three-law-evidence-diagnostic/v1"
    assert manifest["scope"] == "DIAGNOSTIC_ONLY_3_OF_58"
    assert manifest["release_ready"] is False
    assert manifest["rag_quality_evaluated"] is False
    assert manifest["expected_production_law_count"] == 58
    assert manifest["observed_target_law_count"] == 3
    assert manifest["observed_article_count"] == 1_007
    assert manifest["qdrant_point_count"] == 1_007
    assert manifest["neo4j_article_count"] == 1_007
    assert manifest["law_article_manifest_sha256"] == (observation.law_article_manifest_sha256)
    assert manifest["postgres_qdrant_ids_sha256"] == (manifest["qdrant_point_ids_sha256"])
    assert manifest["postgres_graph_ids_sha256"] == (manifest["neo4j_article_ids_sha256"])
    assert manifest["runtime_stability"] == {
        "omp_num_threads": "1",
        "mkl_num_threads": "1",
        "tokenizers_parallelism": "false",
        "hf_hub_offline": "1",
    }
    assert manifest["data_revision"] == _REVISION
    assert manifest["evidence_runtime_release"] == identity


def test_store_drift_fails_closed_before_readiness_or_retrieval(tmp_path: Path) -> None:
    observations = iter((_observation(), _observation(article_count=1_008)))
    app = _app(tmp_path, observation_loader=lambda _deadline=None: next(observations))

    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51003),
    ) as client:
        response = client.get(
            RUNTIME_READINESS_PATH,
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert app.state.release_ready is False


def test_lease_assertion_reobserves_after_backend_work() -> None:
    initial = _observation()
    changed = _observation(article_count=1_008)
    observations = iter((initial, changed))
    lease = runner.derive_three_law_runtime_lease(_provider_lock(), initial)
    loader = runner._ObservedDiagnosticRuntimeLeaseLoader(
        lease,
        initial_observation=initial,
        observation_loader=lambda _deadline=None: next(observations),
        executing_repository_attestor=lambda _deadline=None: None,
        expected_loaded_law_count=3,
    )

    loader.assert_unchanged(lease)
    with pytest.raises(runner.RuntimeReleaseError, match="changed during request"):
        loader.assert_unchanged(lease)


def test_manifest_is_location_free_redacted_and_refuses_unsafe_paths(tmp_path: Path) -> None:
    observation = _observation()
    lease = runner.derive_three_law_runtime_lease(_provider_lock(), observation)
    manifest = runner.ThreeLawDiagnosticManifest(
        data_revision=_REVISION,
        observation=observation,
        identity=lease.identity,
    )
    context = tmp_path / "provider-context"
    context.mkdir()
    output_root = tmp_path / "outside"
    output_root.mkdir()
    output = output_root / "manifest.json"

    runner.write_diagnostic_manifest(
        manifest,
        output,
        forbidden_roots=(runner.ROOT, context),
    )
    raw = output.read_text(encoding="utf-8")
    payload = json.loads(raw)
    assert _TOKEN not in raw
    assert str(tmp_path) not in raw
    assert "evidence_base_url" not in payload
    assert payload["database_access"] == "read_only"
    with pytest.raises(runner.ThreeLawDiagnosticError, match="new regular"):
        runner.write_diagnostic_manifest(
            manifest,
            output,
            forbidden_roots=(runner.ROOT, context),
        )
    with pytest.raises(runner.ThreeLawDiagnosticError, match="outside"):
        runner.write_diagnostic_manifest(
            manifest,
            context / "manifest.json",
            forbidden_roots=(runner.ROOT, context),
        )
    with pytest.raises(runner.ThreeLawDiagnosticError, match="outside"):
        runner.write_diagnostic_manifest(
            manifest,
            runner.ROOT / "forbidden-three-law-manifest.json",
            forbidden_roots=(runner.ROOT, context),
        )


def test_main_redacts_configuration_paths_and_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = runner.main(
        [
            "--diagnostic-only-three-law",
            "--provider-context",
            str(tmp_path / "private-provider-context"),
            "--service-token-file",
            str(tmp_path / "private-service-token"),
            "--diagnostic-manifest-output",
            str(tmp_path / "private-manifest.json"),
            "--host",
            "127.0.0.1",
            "--port",
            "18012",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 78
    assert "Traceback" not in captured.err
    assert '"reason": "invalid_configuration"' in captured.err
    assert str(tmp_path) not in captured.err
    assert _TOKEN not in captured.err
