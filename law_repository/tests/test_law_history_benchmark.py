from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from src.law_history_benchmark import (
    LATENCY_EXEMPT_CANDIDATES,
    BASELINE_CANDIDATE,
    BENCHMARK_RESULT_SCHEMA_VERSION,
    CPU_RERANK_CANDIDATE,
    FIVE_STAGE_CPU_CANDIDATE,
    GPU_RERANK_CANDIDATE,
    KIWI_CANDIDATE,
    CandidateMetrics,
    HeldOutApproval,
    HeldOutCase,
    HeldOutSuite,
    LawHistoryBenchmarkError,
    benchmark_application_from_bytes,
    benchmark_result_payload,
    choose_candidate,
    choose_deployable_candidate,
    measure_candidate,
    parse_held_out_suite,
    validate_held_out_approval,
)


def _benchmark_tool() -> ModuleType:
    tool_path = Path(__file__).resolve().parents[2] / "tools" / "benchmark_law_history_retrieval.py"
    spec = importlib.util.spec_from_file_location("law_history_benchmark_tool_test", tool_path)
    if spec is None or spec.loader is None:
        raise AssertionError("benchmark tool cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline() -> CandidateMetrics:
    return CandidateMetrics("rrf_lexical", True, 0.8, 0.9, 100.0, 1000, 2)


def test_candidate_selection_rejects_recall_or_latency_regression() -> None:
    decision = choose_candidate(
        baseline=_baseline(),
        candidates=(
            CandidateMetrics("bad-recall", True, 0.7, 0.9, 20.0, 10, 2),
            CandidateMetrics("bad-latency", True, 0.8, 0.9, 111.0, 10, 2),
        ),
    )
    assert decision.selected == "rrf_lexical"


def test_candidate_selection_uses_latency_then_memory_and_keeps_exact_tie() -> None:
    selected = choose_candidate(
        baseline=_baseline(),
        candidates=(
            CandidateMetrics("same", True, 0.8, 0.9, 100.0, 1000, 2),
            CandidateMetrics("faster", True, 0.8, 0.9, 99.0, 5000, 2),
        ),
    )
    assert selected.selected == "faster"

    tied = choose_candidate(
        baseline=_baseline(),
        candidates=(CandidateMetrics("same", True, 0.8, 0.9, 100.0, 1000, 2),),
    )
    assert tied.selected == "rrf_lexical"


def test_measurement_uses_strict_top_k_recall_and_has_no_query_output() -> None:
    cases = (
        HeldOutCase("case-1", "private-query-alpha", ("law-a",)),
        HeldOutCase("case-2", "private-query-beta", ("law-b", "law-c")),
    )
    metrics = measure_candidate(
        candidate="test",
        cases=cases,
        retrieve_law_ids=lambda query, _limit: (
            ("law-a",) if query == "private-query-alpha" else ("law-b", "law-c")
        ),
    )
    assert metrics.available is True
    assert metrics.strict_recall_at_5 == 1.0
    assert metrics.strict_recall_at_10 == 1.0
    assert "private-query-alpha" not in str(metrics.safe_dict())

    partial = measure_candidate(
        candidate="partial-targets",
        cases=(HeldOutCase("case-3", "three", ("law-a", "law-b")),),
        retrieve_law_ids=lambda _query, _limit: ("law-a",),
    )
    assert partial.strict_recall_at_5 == 0.0
    assert partial.strict_recall_at_10 == 0.0


def test_measurement_uses_process_rss_so_native_memory_participates_in_tie_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.law_history_benchmark as benchmark

    # The first sample is taken before retrieval, then once per held-out
    # case.  These stand in for an OS RSS probe rather than tracemalloc.
    samples = iter((1_000, 4_000, 3_000))
    monkeypatch.setattr(benchmark, "_process_resident_memory_bytes", lambda: next(samples))

    metrics = measure_candidate(
        candidate=CPU_RERANK_CANDIDATE,
        cases=(
            HeldOutCase("case-rss-1", "one", ("law-a",)),
            HeldOutCase("case-rss-2", "two", ("law-b",)),
        ),
        retrieve_law_ids=lambda query, _limit: ("law-a",) if query == "one" else ("law-b",),
    )

    assert metrics.available is True
    assert metrics.peak_memory_bytes == 4_000
    assert metrics.peak_gpu_memory_bytes is None
    assert metrics.resolved_device_mode == "cpu"


def test_measurement_fails_closed_when_native_process_memory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.law_history_benchmark as benchmark

    monkeypatch.setattr(benchmark, "_process_resident_memory_bytes", lambda: None)

    metrics = measure_candidate(
        candidate=CPU_RERANK_CANDIDATE,
        cases=(HeldOutCase("case-rss-unavailable", "one", ("law-a",)),),
        retrieve_law_ids=lambda _query, _limit: ("law-a",),
    )

    assert metrics.available is False
    assert metrics.peak_memory_bytes is None
    assert metrics.resolved_device_mode == "cpu"


def test_gpu_measurement_records_cuda_allocation_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.law_history_benchmark as benchmark

    samples = iter((2_000, 3_000))
    monkeypatch.setattr(benchmark, "_process_resident_memory_bytes", lambda: next(samples))
    monkeypatch.setattr(benchmark, "_prepare_cuda_peak_memory_probe", lambda: True)
    monkeypatch.setattr(benchmark, "_cuda_peak_memory_bytes", lambda: 7_000)

    metrics = measure_candidate(
        candidate=GPU_RERANK_CANDIDATE,
        cases=(HeldOutCase("case-gpu", "one", ("law-a",)),),
        retrieve_law_ids=lambda _query, _limit: ("law-a",),
        device_mode="cuda",
    )

    assert metrics.available is True
    assert metrics.peak_memory_bytes == 3_000
    assert metrics.peak_gpu_memory_bytes == 7_000
    assert metrics.resolved_device_mode == "cuda"


def test_gpu_measurement_is_unavailable_without_a_separate_cuda_allocation_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.law_history_benchmark as benchmark

    samples = iter((2_000, 3_000))
    monkeypatch.setattr(benchmark, "_process_resident_memory_bytes", lambda: next(samples))
    monkeypatch.setattr(benchmark, "_prepare_cuda_peak_memory_probe", lambda: True)
    monkeypatch.setattr(benchmark, "_cuda_peak_memory_bytes", lambda: None)

    metrics = measure_candidate(
        candidate=GPU_RERANK_CANDIDATE,
        cases=(HeldOutCase("case-gpu-unavailable", "one", ("law-a",)),),
        retrieve_law_ids=lambda _query, _limit: ("law-a",),
        device_mode="cuda",
    )

    assert metrics.available is False
    assert metrics.peak_memory_bytes is None
    assert metrics.peak_gpu_memory_bytes is None
    assert metrics.resolved_device_mode == "cuda"


def _benchmark_metrics(
    *,
    cpu: CandidateMetrics | None = None,
    five_stage: CandidateMetrics | None = None,
    kiwi: CandidateMetrics | None = None,
    gpu: CandidateMetrics | None = None,
) -> list[CandidateMetrics]:
    return [
        CandidateMetrics(BASELINE_CANDIDATE, True, 0.8, 0.9, 100.0, 1000, 2),
        cpu or CandidateMetrics(CPU_RERANK_CANDIDATE, False, None, None, None, None, 2),
        five_stage or CandidateMetrics(FIVE_STAGE_CPU_CANDIDATE, False, None, None, None, None, 2),
        kiwi or CandidateMetrics(KIWI_CANDIDATE, False, None, None, None, None, 2),
        gpu or CandidateMetrics(GPU_RERANK_CANDIDATE, False, None, None, None, None, 2),
    ]


def _approved_held_out_suite(
    *, suite_id: str = "law-history-heldout-v2", law_as_of: str = "2026-08-05"
) -> tuple[HeldOutSuite, HeldOutApproval, bytes, bytes]:
    suite_raw = json.dumps(
        {
            "schema_version": "2.0",
            "suite_id": suite_id,
            "cases": [
                {
                    "case_id": "held-out-1",
                    "query": "private-held-out-query",
                    "expected_law_ids": ["law-a"],
                },
                {
                    "case_id": "held-out-2",
                    "query": "private-held-out-query-two",
                    "expected_law_ids": ["law-b"],
                },
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suite = parse_held_out_suite(suite_raw)
    receipt_raw = json.dumps(
        {
            "schema_version": "law-history-held-out-approval/v2",
            "approval_id": "review-20260810",
            "approved_at": "2026-08-10T00:00:00+00:00",
            "approval_status": "approved",
            "suite_id": suite.suite_id,
            "suite_sha256": suite.suite_sha256,
            "case_count": suite.case_count,
            "law_as_of": law_as_of,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    approval = validate_held_out_approval(suite=suite, receipt_raw=receipt_raw)
    return suite, approval, suite_raw, receipt_raw


def _benchmark_bytes(
    metrics: list[CandidateMetrics], *, held_out_approval: HeldOutApproval | None = None
) -> bytes:
    decision = choose_deployable_candidate(
        baseline=metrics[0], cpu_rerank=metrics[1], five_stage=metrics[2]
    )
    approval = held_out_approval or _approved_held_out_suite()[1]
    payload = benchmark_result_payload(
        metrics=metrics,
        decision=decision,
        active_manifest_sha256="a" * 64,
        held_out_approval=approval,
        generated_at="2026-08-10T00:00:00+00:00",
    )
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_approved_suite(
    tmp_path: Path, *, law_as_of: str = "2026-08-05"
) -> tuple[Path, Path, HeldOutApproval]:
    _suite, approval, suite_raw, receipt_raw = _approved_held_out_suite(law_as_of=law_as_of)
    suite_path = tmp_path / "data" / "law-history-heldout" / "suite.json"
    receipt_path = suite_path.parent / "approvals" / "review.json"
    suite_path.parent.mkdir(parents=True)
    receipt_path.parent.mkdir(parents=True)
    suite_path.write_bytes(suite_raw)
    receipt_path.write_bytes(receipt_raw)
    return suite_path, receipt_path, approval


def test_held_out_approval_binds_suite_id_exact_bytes_and_case_count_without_queries() -> None:
    suite, approval, suite_raw, receipt_raw = _approved_held_out_suite()

    assert approval.suite_id == suite.suite_id
    assert approval.suite_sha256 == hashlib.sha256(suite_raw).hexdigest()
    assert approval.case_count == suite.case_count == 2
    assert approval.law_as_of.isoformat() == "2026-08-05"
    assert "private-held-out-query" not in receipt_raw.decode("utf-8")
    assert "private-held-out-query" not in json.dumps(approval.result_dict())

    whitespace_changed_suite = parse_held_out_suite(suite_raw + b"\n")
    with pytest.raises(LawHistoryBenchmarkError, match="APPROVAL_MISMATCH"):
        validate_held_out_approval(suite=whitespace_changed_suite, receipt_raw=receipt_raw)

    receipt_with_query = json.loads(receipt_raw)
    receipt_with_query["queries"] = ["private-held-out-query"]
    with pytest.raises(LawHistoryBenchmarkError, match="APPROVAL_INVALID"):
        validate_held_out_approval(
            suite=suite, receipt_raw=json.dumps(receipt_with_query).encode("utf-8")
        )

    date_unbound_receipt = json.loads(receipt_raw)
    date_unbound_receipt["schema_version"] = "law-history-held-out-approval/v1"
    date_unbound_receipt.pop("law_as_of")
    with pytest.raises(LawHistoryBenchmarkError, match="APPROVAL_INVALID"):
        validate_held_out_approval(
            suite=suite,
            receipt_raw=json.dumps(date_unbound_receipt, sort_keys=True).encode("utf-8"),
        )

    with pytest.raises(LawHistoryBenchmarkError, match="HELD_OUT_INVALID"):
        parse_held_out_suite(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "provenance": "independently_reviewed_held_out",
                    "cases": [],
                }
            ).encode("utf-8")
        )


def test_benchmark_result_keeps_only_approved_suite_receipt_and_apply_revalidates_it() -> None:
    suite, approval, _suite_raw, receipt_raw = _approved_held_out_suite()
    raw = _benchmark_bytes(_benchmark_metrics(), held_out_approval=approval)

    assert "private-held-out-query" not in raw.decode("utf-8")
    receipt_changed = json.loads(receipt_raw)
    receipt_changed["approval_id"] = "review-20260811"
    changed_approval = validate_held_out_approval(
        suite=suite, receipt_raw=json.dumps(receipt_changed, sort_keys=True).encode("utf-8")
    )
    with pytest.raises(LawHistoryBenchmarkError, match="APPROVAL_MISMATCH"):
        benchmark_application_from_bytes(raw, held_out_approval=changed_approval)


def test_benchmark_result_binds_the_exact_approved_law_evaluation_date() -> None:
    approval = _approved_held_out_suite(law_as_of="2026-08-05")[1]
    raw = _benchmark_bytes(_benchmark_metrics(), held_out_approval=approval)
    payload = json.loads(raw)

    assert payload["evaluation_law_as_of"] == "2026-08-05"
    payload["evaluation_law_as_of"] = "2026-08-06"
    with pytest.raises(LawHistoryBenchmarkError, match="APPROVAL_MISMATCH"):
        benchmark_application_from_bytes(
            json.dumps(payload, sort_keys=True).encode("utf-8"),
            held_out_approval=approval,
        )


def test_benchmark_result_records_cpu_and_explicit_gpu_or_not_run_modes() -> None:
    approval = _approved_held_out_suite()[1]
    payload = benchmark_result_payload(
        metrics=_benchmark_metrics(),
        decision=choose_deployable_candidate(
            baseline=_benchmark_metrics()[0],
            cpu_rerank=_benchmark_metrics()[1],
            five_stage=_benchmark_metrics()[2],
        ),
        active_manifest_sha256="a" * 64,
        held_out_approval=approval,
        generated_at="2026-08-10T00:00:00+00:00",
    )
    assert payload["schema_version"] == BENCHMARK_RESULT_SCHEMA_VERSION == "5.0"
    raw_metrics = payload["metrics"]
    assert isinstance(raw_metrics, list)
    assert all(isinstance(metric, dict) for metric in raw_metrics)
    metrics = [metric for metric in raw_metrics if isinstance(metric, dict)]
    modes = {metric["candidate"]: metric["device_mode"] for metric in metrics}
    assert modes[BASELINE_CANDIDATE] == "cpu"
    assert modes[CPU_RERANK_CANDIDATE] == "cpu"
    assert modes[FIVE_STAGE_CPU_CANDIDATE] == "cpu"
    assert modes[KIWI_CANDIDATE] == "cpu"
    assert modes[GPU_RERANK_CANDIDATE] == "not_run"

    legacy_memory_payload = dict(payload)
    legacy_memory_payload["schema_version"] = "3.0"
    with pytest.raises(LawHistoryBenchmarkError, match="RESULT_INVALID"):
        benchmark_application_from_bytes(
            json.dumps(legacy_memory_payload, sort_keys=True).encode("utf-8"),
            held_out_approval=approval,
        )

    metrics[0]["device_mode"] = "cuda"
    with pytest.raises(LawHistoryBenchmarkError, match="RESULT_INVALID"):
        benchmark_application_from_bytes(
            json.dumps(payload, sort_keys=True).encode("utf-8"), held_out_approval=approval
        )


def test_benchmark_result_rejects_metrics_not_measured_on_the_approved_case_count() -> None:
    _suite, approval, _suite_raw, _receipt_raw = _approved_held_out_suite()
    metrics = [
        CandidateMetrics(BASELINE_CANDIDATE, True, 0.8, 0.9, 100.0, 1000, 1),
        CandidateMetrics(CPU_RERANK_CANDIDATE, False, None, None, None, None, 1),
        CandidateMetrics(FIVE_STAGE_CPU_CANDIDATE, False, None, None, None, None, 1),
        CandidateMetrics(KIWI_CANDIDATE, False, None, None, None, None, 1),
        CandidateMetrics(GPU_RERANK_CANDIDATE, False, None, None, None, None, 1),
    ]
    with pytest.raises(LawHistoryBenchmarkError, match="APPROVAL_MISMATCH"):
        benchmark_result_payload(
            metrics=metrics,
            decision=choose_deployable_candidate(
                baseline=metrics[0], cpu_rerank=metrics[1], five_stage=metrics[2]
            ),
            active_manifest_sha256="a" * 64,
            held_out_approval=approval,
            generated_at="2026-08-10T00:00:00+00:00",
        )


def test_apply_parser_rejects_a_tampered_result_case_count() -> None:
    _suite, approval, _suite_raw, _receipt_raw = _approved_held_out_suite()
    raw = _benchmark_bytes(_benchmark_metrics(), held_out_approval=approval)
    payload = json.loads(raw)
    for metric in payload["metrics"]:
        metric["case_count"] = 1

    with pytest.raises(LawHistoryBenchmarkError, match="APPROVAL_MISMATCH"):
        benchmark_application_from_bytes(
            json.dumps(payload, sort_keys=True).encode("utf-8"), held_out_approval=approval
        )


def test_benchmark_application_only_binds_a_deployable_cpu_rerank_result() -> None:
    raw = _benchmark_bytes(
        _benchmark_metrics(cpu=CandidateMetrics(CPU_RERANK_CANDIDATE, True, 0.8, 0.9, 99.0, 900, 2))
    )

    application = benchmark_application_from_bytes(
        raw, held_out_approval=_approved_held_out_suite()[1]
    )

    assert application.selected_candidate == CPU_RERANK_CANDIDATE
    assert application.retrieval_strategy == "cross_encoder_rerank"
    assert application.expected_manifest_sha256 == "a" * 64
    assert application.benchmark_result_sha256


@pytest.mark.parametrize(
    ("candidate", "metrics"),
    [
        (
            KIWI_CANDIDATE,
            _benchmark_metrics(
                kiwi=CandidateMetrics(
                    KIWI_CANDIDATE,
                    True,
                    0.9,
                    0.9,
                    99.0,
                    900,
                    2,
                    "b" * 64,
                )
            ),
        ),
        (
            GPU_RERANK_CANDIDATE,
            _benchmark_metrics(
                gpu=CandidateMetrics(
                    GPU_RERANK_CANDIDATE,
                    True,
                    0.9,
                    0.9,
                    99.0,
                    900,
                    2,
                    peak_gpu_memory_bytes=800,
                )
            ),
        ),
    ],
)
def test_nondeployable_kiwi_and_gpu_metrics_are_diagnostic_not_promotion_candidates(
    candidate: str, metrics: list[CandidateMetrics]
) -> None:
    raw = _benchmark_bytes(metrics)

    application = benchmark_application_from_bytes(
        raw, held_out_approval=_approved_held_out_suite()[1]
    )

    assert application.selected_candidate == BASELINE_CANDIDATE
    # A generic all-candidate comparison would choose the diagnostic result,
    # but its sequential local memory measurement cannot affect deployment.
    all_candidate_decision = choose_candidate(baseline=metrics[0], candidates=metrics[1:])
    assert all_candidate_decision.selected == candidate
    with pytest.raises(LawHistoryBenchmarkError, match="DECISION_INVALID"):
        benchmark_result_payload(
            metrics=metrics,
            decision=all_candidate_decision,
            active_manifest_sha256="a" * 64,
            held_out_approval=_approved_held_out_suite()[1],
            generated_at="2026-08-10T00:00:00+00:00",
        )


def test_benchmark_application_recomputes_and_rejects_a_tampered_decision() -> None:
    raw = _benchmark_bytes(_benchmark_metrics())
    payload = json.loads(raw)
    payload["decision"]["selected"] = CPU_RERANK_CANDIDATE

    with pytest.raises(LawHistoryBenchmarkError, match="RESULT_INVALID"):
        benchmark_application_from_bytes(
            json.dumps(payload).encode("utf-8"), held_out_approval=_approved_held_out_suite()[1]
        )


def test_cli_apply_binds_only_a_verified_cpu_result_via_law_history_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src import law_history

    tool = _benchmark_tool()
    held_out, approval_receipt, approval = _write_approved_suite(tmp_path)
    raw = _benchmark_bytes(
        _benchmark_metrics(
            cpu=CandidateMetrics(CPU_RERANK_CANDIDATE, True, 0.8, 0.9, 99.0, 900, 2)
        ),
        held_out_approval=approval,
    )
    result = tmp_path / "state" / "law-history-benchmarks" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_bytes(raw)
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "validate_law_history_state",
        lambda: ({"coverage_through": "2026-08-05"}, "a" * 64),
    )
    captured: dict[str, object] = {}

    def bind(**kwargs: object) -> str:
        captured.update(kwargs)
        return "c" * 64

    monkeypatch.setattr(law_history, "bind_law_history_retrieval_strategy", bind)

    assert (
        tool.main(
            [
                "--apply-result",
                str(result),
                "--held-out",
                str(held_out),
                "--approval-receipt",
                str(approval_receipt),
            ]
        )
        == 0
    )
    assert captured == {
        "strategy": "cross_encoder_rerank",
        "expected_manifest_sha256": "a" * 64,
        "benchmark_result_sha256": hashlib.sha256(raw).hexdigest(),
    }
    assert "retrieval strategy applied" in capsys.readouterr().out


def test_cli_apply_keeps_kiwi_measurement_diagnostic_and_binds_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src import law_history

    tool = _benchmark_tool()
    held_out, approval_receipt, approval = _write_approved_suite(tmp_path)
    raw = _benchmark_bytes(
        _benchmark_metrics(
            kiwi=CandidateMetrics(
                KIWI_CANDIDATE,
                True,
                0.9,
                0.9,
                99.0,
                900,
                2,
                "b" * 64,
            )
        ),
        held_out_approval=approval,
    )
    result = tmp_path / "state" / "law-history-benchmarks" / "kiwi-result.json"
    result.parent.mkdir(parents=True)
    result.write_bytes(raw)
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "validate_law_history_state",
        lambda: ({"coverage_through": "2026-08-05"}, "a" * 64),
    )
    captured: dict[str, object] = {}

    def bind(**kwargs: object) -> str:
        captured.update(kwargs)
        return "c" * 64

    monkeypatch.setattr(law_history, "bind_law_history_retrieval_strategy", bind)

    assert (
        tool.main(
            [
                "--apply-result",
                str(result),
                "--held-out",
                str(held_out),
                "--approval-receipt",
                str(approval_receipt),
            ]
        )
        == 0
    )
    assert captured["strategy"] == "rrf_lexical"
    assert "retrieval strategy applied" in capsys.readouterr().out


def test_cli_apply_reports_a_stale_manifest_without_exposing_result_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src import law_history

    tool = _benchmark_tool()
    held_out, approval_receipt, approval = _write_approved_suite(tmp_path)
    raw = _benchmark_bytes(
        _benchmark_metrics(
            cpu=CandidateMetrics(CPU_RERANK_CANDIDATE, True, 0.8, 0.9, 99.0, 900, 2)
        ),
        held_out_approval=approval,
    )
    result = tmp_path / "state" / "law-history-benchmarks" / "stale-result.json"
    result.parent.mkdir(parents=True)
    result.write_bytes(raw)
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "validate_law_history_state",
        lambda: ({"coverage_through": "2026-08-05"}, "a" * 64),
    )

    def stale_bind(**_kwargs: object) -> str:
        raise law_history.LawHistoryError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")

    monkeypatch.setattr(law_history, "bind_law_history_retrieval_strategy", stale_bind)

    assert (
        tool.main(
            [
                "--apply-result",
                str(result),
                "--held-out",
                str(held_out),
                "--approval-receipt",
                str(approval_receipt),
            ]
        )
        == 2
    )
    stderr = capsys.readouterr().err
    assert "BENCHMARK_MANIFEST_STALE" in stderr
    assert "private-query" not in stderr


def test_cli_apply_refuses_a_result_when_refresh_changed_the_evaluation_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src import law_history

    tool = _benchmark_tool()
    held_out, approval_receipt, approval = _write_approved_suite(tmp_path, law_as_of="2026-08-05")
    raw = _benchmark_bytes(
        _benchmark_metrics(
            cpu=CandidateMetrics(CPU_RERANK_CANDIDATE, True, 0.8, 0.9, 99.0, 900, 2)
        ),
        held_out_approval=approval,
    )
    result = tmp_path / "state" / "law-history-benchmarks" / "refresh-date-drift.json"
    result.parent.mkdir(parents=True)
    result.write_bytes(raw)
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "validate_law_history_state",
        lambda: ({"coverage_through": "2026-08-06"}, "a" * 64),
    )

    def unexpected_bind(**_kwargs: object) -> str:
        raise AssertionError("a refreshed evaluation date must not reach manifest binding")

    monkeypatch.setattr(law_history, "bind_law_history_retrieval_strategy", unexpected_bind)

    assert (
        tool.main(
            [
                "--apply-result",
                str(result),
                "--held-out",
                str(held_out),
                "--approval-receipt",
                str(approval_receipt),
            ]
        )
        == 2
    )
    assert "BENCHMARK_MANIFEST_STALE" in capsys.readouterr().err


def test_cli_measurement_requires_the_active_coverage_date_in_the_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _benchmark_tool()
    held_out, approval_receipt, _approval = _write_approved_suite(tmp_path, law_as_of="2026-08-05")
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "verify_law_history_store",
        lambda: {
            "manifest_sha256": "a" * 64,
            "source_article_projection_sha256": "s" * 64,
            "temporal_store_projection_sha256": "p" * 64,
        },
    )
    monkeypatch.setattr(
        tool,
        "validate_law_history_state",
        lambda: (
            {
                "source_article_projection_sha256": "s" * 64,
                "temporal_store_projection_sha256": "p" * 64,
                "coverage_through": "2026-08-06",
            },
            "a" * 64,
        ),
    )
    monkeypatch.setattr(
        tool,
        "FunctionalLawHistoryEvidenceBackend",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("date mismatch must stop first")),
    )

    assert (
        tool.main(["--held-out", str(held_out), "--approval-receipt", str(approval_receipt)]) == 2
    )
    assert "EVALUATION_AS_OF_MISMATCH" in capsys.readouterr().err


def test_cli_measurement_uses_active_coverage_date_for_every_law_retriever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _benchmark_tool()
    held_out, approval_receipt, approval = _write_approved_suite(tmp_path, law_as_of="2026-08-06")
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "verify_law_history_store",
        lambda: {
            "manifest_sha256": "a" * 64,
            "source_article_projection_sha256": "s" * 64,
            "temporal_store_projection_sha256": "p" * 64,
        },
    )
    monkeypatch.setattr(
        tool,
        "validate_law_history_state",
        lambda: (
            {
                "source_article_projection_sha256": "s" * 64,
                "temporal_store_projection_sha256": "p" * 64,
                "coverage_through": "2026-08-06",
            },
            "a" * 64,
        ),
    )
    seen_as_of: list[tuple[str, date]] = []

    class FakeBackend:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def search_law_articles(self, _query: str, _limit: int, _as_of: date) -> tuple[object, ...]:
            return ()

    class FakeKiwi:
        encoder_fingerprint = "b" * 64

    def fake_measure(
        *,
        candidate: str,
        cases: tuple[HeldOutCase, ...],
        retrieve_law_ids: object,
        device_mode: str | None = None,
    ) -> CandidateMetrics:
        as_of = getattr(retrieve_law_ids, "_as_of", None)
        if isinstance(as_of, date):
            seen_as_of.append((candidate, as_of))
        case_count = len(cases)
        if candidate == GPU_RERANK_CANDIDATE:
            return CandidateMetrics(candidate, False, None, None, None, None, case_count)
        return CandidateMetrics(
            candidate,
            True,
            0.8,
            0.9,
            10.0,
            100,
            case_count,
        )

    def fake_kiwi_retriever(*, as_of: date) -> tuple[FakeKiwi, object]:
        seen_as_of.append((KIWI_CANDIDATE, as_of))
        return FakeKiwi(), lambda _query, _limit: ()

    monkeypatch.setattr(tool, "FunctionalLawHistoryEvidenceBackend", FakeBackend)
    monkeypatch.setattr(tool, "measure_candidate", fake_measure)
    monkeypatch.setattr(tool, "_kiwi_retriever", fake_kiwi_retriever)
    monkeypatch.setattr(tool, "_write_metrics", lambda **_kwargs: tmp_path / "result.json")

    assert (
        tool.main(["--held-out", str(held_out), "--approval-receipt", str(approval_receipt)]) == 0
    )
    assert seen_as_of == [
        (BASELINE_CANDIDATE, date(2026, 8, 6)),
        (CPU_RERANK_CANDIDATE, date(2026, 8, 6)),
        (FIVE_STAGE_CPU_CANDIDATE, date(2026, 8, 6)),
        (KIWI_CANDIDATE, date(2026, 8, 6)),
    ]
    assert approval.law_as_of == date(2026, 8, 6)


def test_cli_apply_revalidates_the_current_query_free_approval_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from src import law_history

    tool = _benchmark_tool()
    held_out, approval_receipt, approval = _write_approved_suite(tmp_path)
    raw = _benchmark_bytes(
        _benchmark_metrics(
            cpu=CandidateMetrics(CPU_RERANK_CANDIDATE, True, 0.8, 0.9, 99.0, 900, 2)
        ),
        held_out_approval=approval,
    )
    result = tmp_path / "state" / "law-history-benchmarks" / "approval-drift.json"
    result.parent.mkdir(parents=True)
    result.write_bytes(raw)
    receipt = json.loads(approval_receipt.read_text(encoding="utf-8"))
    receipt["approval_id"] = "review-20260811"
    approval_receipt.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)

    def unexpected_bind(**_kwargs: object) -> str:
        raise AssertionError("changed approval must never reach the manifest API")

    monkeypatch.setattr(law_history, "bind_law_history_retrieval_strategy", unexpected_bind)

    assert (
        tool.main(
            [
                "--apply-result",
                str(result),
                "--held-out",
                str(held_out),
                "--approval-receipt",
                str(approval_receipt),
            ]
        )
        == 2
    )
    assert "APPROVAL_MISMATCH" in capsys.readouterr().err


def test_cli_requires_the_suite_and_query_free_approval_for_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _benchmark_tool()
    result = tmp_path / "state" / "law-history-benchmarks" / "result.json"
    result.parent.mkdir(parents=True)
    result.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)

    with pytest.raises(SystemExit, match="2"):
        tool.main(["--apply-result", str(result)])


def test_strategy_binding_requires_a_full_temporal_projection_audit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from src import law_history

    manifest = {
        "retrieval_strategy": "rrf_lexical",
        "retrieval_strategy_benchmark_sha256": None,
        "source_article_projection_sha256": "s" * 64,
        "temporal_store_projection_sha256": "p" * 64,
    }
    monkeypatch.setattr(
        law_history,
        "verify_law_history_store",
        lambda **_kwargs: {
            "manifest_sha256": "a" * 64,
            "source_article_projection_sha256": "s" * 64,
            "temporal_store_projection_sha256": "p" * 64,
        },
    )
    monkeypatch.setattr(
        law_history,
        "validate_law_history_state",
        lambda **_kwargs: (manifest, "a" * 64),
    )
    monkeypatch.setattr(
        law_history, "ensure_functional_uat_law_history_profile", lambda **_kwargs: tmp_path
    )
    written: dict[str, object] = {}
    monkeypatch.setattr(
        law_history,
        "_replace_active_manifest_if_digest",
        lambda **kwargs: written.update(kwargs["manifest"]) or "b" * 64,
    )

    assert (
        law_history.bind_law_history_retrieval_strategy(
            strategy="cross_encoder_rerank",
            expected_manifest_sha256="a" * 64,
            benchmark_result_sha256="c" * 64,
        )
        == "b" * 64
    )
    assert written["retrieval_strategy"] == "cross_encoder_rerank"
    assert written["retrieval_strategy_benchmark_sha256"] == "c" * 64


def test_strategy_binding_rejects_a_benchmark_without_matching_full_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import law_history

    monkeypatch.setattr(
        law_history,
        "verify_law_history_store",
        lambda **_kwargs: {
            "manifest_sha256": "a" * 64,
            "source_article_projection_sha256": "s" * 64,
            "temporal_store_projection_sha256": "p" * 64,
        },
    )
    monkeypatch.setattr(
        law_history,
        "validate_law_history_state",
        lambda **_kwargs: (
            {
                "source_article_projection_sha256": "s" * 64,
                "temporal_store_projection_sha256": "q" * 64,
            },
            "a" * 64,
        ),
    )

    with pytest.raises(law_history.LawHistoryError, match="BENCHMARK_MANIFEST_STALE"):
        law_history.bind_law_history_retrieval_strategy(
            strategy="rrf_lexical",
            expected_manifest_sha256="a" * 64,
            benchmark_result_sha256="b" * 64,
        )


def test_cli_measurement_rejects_a_full_projection_that_is_not_the_active_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = _benchmark_tool()
    held_out, approval_receipt, _approval = _write_approved_suite(tmp_path)
    monkeypatch.setattr(tool, "local_root", lambda: tmp_path)
    monkeypatch.setattr(
        tool,
        "verify_law_history_store",
        lambda: {
            "manifest_sha256": "b" * 64,
            "source_article_projection_sha256": "s" * 64,
            "temporal_store_projection_sha256": "p" * 64,
        },
    )
    monkeypatch.setattr(
        tool,
        "validate_law_history_state",
        lambda: (
            {
                "source_article_projection_sha256": "s" * 64,
                "temporal_store_projection_sha256": "p" * 64,
            },
            "a" * 64,
        ),
    )

    assert (
        tool.main(["--held-out", str(held_out), "--approval-receipt", str(approval_receipt)]) == 2
    )
    stderr = capsys.readouterr().err
    assert "BENCHMARK_MANIFEST_STALE" in stderr
    assert "private-query" not in stderr


def test_five_stage_candidate_is_gated_by_latency_like_any_other() -> None:
    """The product pipeline is promoted only when recall and p95 both hold."""

    slower_but_better = CandidateMetrics(FIVE_STAGE_CPU_CANDIDATE, True, 0.85, 0.95, 400.0, 2000, 2)
    decision = choose_candidate(baseline=_baseline(), candidates=(slower_but_better,))
    assert decision.selected == BASELINE_CANDIDATE
    assert decision.reason == "no_candidate_passed_non_regression"

    within_budget = CandidateMetrics(FIVE_STAGE_CPU_CANDIDATE, True, 0.85, 0.95, 105.0, 2000, 2)
    assert (
        choose_candidate(baseline=_baseline(), candidates=(within_budget,)).selected
        == FIVE_STAGE_CPU_CANDIDATE
    )

    faster_but_worse = CandidateMetrics(FIVE_STAGE_CPU_CANDIDATE, True, 0.75, 0.95, 105.0, 2000, 2)
    assert (
        choose_candidate(baseline=_baseline(), candidates=(faster_but_worse,)).selected
        == BASELINE_CANDIDATE
    )
    # The default exemption set is empty; an explicit local experiment may
    # still opt one candidate out of the latency gate.
    assert LATENCY_EXEMPT_CANDIDATES == frozenset()
    assert (
        choose_candidate(
            baseline=_baseline(),
            candidates=(slower_but_better,),
            latency_exempt=frozenset({FIVE_STAGE_CPU_CANDIDATE}),
        ).selected
        == FIVE_STAGE_CPU_CANDIDATE
    )


def test_deployable_decision_considers_five_stage_and_application_binds_it() -> None:
    five_stage = CandidateMetrics(FIVE_STAGE_CPU_CANDIDATE, True, 0.9, 0.95, 105.0, 2000, 2)
    metrics = _benchmark_metrics(
        cpu=CandidateMetrics(CPU_RERANK_CANDIDATE, True, 0.8, 0.9, 99.0, 900, 2),
        five_stage=five_stage,
    )
    decision = choose_deployable_candidate(
        baseline=metrics[0], cpu_rerank=metrics[1], five_stage=metrics[2]
    )
    assert decision.selected == FIVE_STAGE_CPU_CANDIDATE

    application = benchmark_application_from_bytes(
        _benchmark_bytes(metrics), held_out_approval=_approved_held_out_suite()[1]
    )
    assert application.selected_candidate == FIVE_STAGE_CPU_CANDIDATE
    assert application.retrieval_strategy == "five_stage_cpu"

    with pytest.raises(LawHistoryBenchmarkError, match="CANDIDATES_INVALID"):
        choose_deployable_candidate(
            baseline=metrics[0], cpu_rerank=metrics[1], five_stage=metrics[3]
        )


def test_benchmark_result_requires_the_five_stage_candidate_in_the_fixed_set() -> None:
    """A V4 aggregate (four candidates) can no longer be applied by mistake."""

    raw = _benchmark_bytes(_benchmark_metrics())
    payload = json.loads(raw)
    payload["metrics"] = [
        metric for metric in payload["metrics"] if metric["candidate"] != FIVE_STAGE_CPU_CANDIDATE
    ]
    with pytest.raises(LawHistoryBenchmarkError, match="RESULT_INVALID"):
        benchmark_application_from_bytes(
            json.dumps(payload, sort_keys=True).encode("utf-8"),
            held_out_approval=_approved_held_out_suite()[1],
        )
    legacy = json.loads(raw)
    legacy["schema_version"] = "4.0"
    with pytest.raises(LawHistoryBenchmarkError, match="RESULT_INVALID"):
        benchmark_application_from_bytes(
            json.dumps(legacy, sort_keys=True).encode("utf-8"),
            held_out_approval=_approved_held_out_suite()[1],
        )
