"""Local-only retrieval benchmark primitives for the 65-law history profile.

The clone-side Goldset is deliberately not an acceptance input.  A maintainer
supplies a separately reviewed held-out set below ignored local state, and
this module writes/returns aggregate metrics only.  It never performs model
downloads, query rewriting, or any network request.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Literal, cast


RuntimeRetrievalStrategy = Literal["rrf_lexical", "cross_encoder_rerank", "five_stage_cpu"]

# Candidate identifiers are deliberately stable.  In particular, the Kiwi
# encoder fingerprint belongs in its own field rather than in the candidate
# name, otherwise a result produced with another installed Kiwi release could
# be mistaken for a different deployment strategy.
BASELINE_CANDIDATE = "rrf_lexical"
CPU_RERANK_CANDIDATE = "cross_encoder_rerank_cpu"
GPU_RERANK_CANDIDATE = "cross_encoder_rerank_gpu"
FIVE_STAGE_CPU_CANDIDATE = "five_stage_cpu"
KIWI_CANDIDATE = "kiwi_sparse"
BENCHMARK_CANDIDATES = frozenset(
    {
        BASELINE_CANDIDATE,
        CPU_RERANK_CANDIDATE,
        FIVE_STAGE_CPU_CANDIDATE,
        GPU_RERANK_CANDIDATE,
        KIWI_CANDIDATE,
    }
)
# Every deployable candidate, including the documented five-stage product
# pipeline, must pass the same 1.10x p95 non-regression gate: a candidate is
# promoted only when it satisfies both recall and latency (2026-09-15: the CPU
# five-stage p95 of 110 s versus the 1.9 s baseline exceeded the Agent evidence
# timeout, so an exemption would have selected an unserviceable strategy).
# ``choose_candidate`` keeps an explicit ``latency_exempt`` parameter for local
# experiments only; the default set is empty.
LATENCY_EXEMPT_CANDIDATES: frozenset[str] = frozenset()
BENCHMARK_RESULT_SCHEMA_VERSION = "5.0"
_LOCAL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")


class LawHistoryBenchmarkError(RuntimeError):
    """The local held-out benchmark is malformed or cannot be trusted."""


@dataclass(frozen=True, slots=True)
class HeldOutCase:
    case_id: str
    query: str
    expected_law_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HeldOutSuite:
    """Private held-out questions plus their immutable local byte receipt."""

    suite_id: str
    suite_sha256: str
    cases: tuple[HeldOutCase, ...]

    @property
    def case_count(self) -> int:
        return len(self.cases)


@dataclass(frozen=True, slots=True)
class HeldOutApproval:
    """Query-free human approval binding for one exact held-out suite."""

    suite_id: str
    suite_sha256: str
    case_count: int
    law_as_of: date
    approval_id: str
    approval_receipt_sha256: str

    def result_dict(self) -> dict[str, object]:
        """Safe aggregate fields retained in a benchmark result and apply path."""

        return {
            "suite_id": self.suite_id,
            "suite_sha256": self.suite_sha256,
            "case_count": self.case_count,
            "law_as_of": self.law_as_of.isoformat(),
            "approval_id": self.approval_id,
            "approval_receipt_sha256": self.approval_receipt_sha256,
        }


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    candidate: str
    available: bool
    strict_recall_at_5: float | None
    strict_recall_at_10: float | None
    p95_ms: float | None
    # ``peak_memory_bytes`` is the largest RSS sample taken during the
    # candidate workload, not Python-only allocator bytes.  The adoption
    # tie-break therefore includes retained native model/runtime allocations.
    peak_memory_bytes: int | None
    case_count: int
    encoder_fingerprint: str | None = None
    device_mode: Literal["cpu", "cuda", "not_run"] | None = None
    # CUDA allocation is a separate resource from host RSS.  It is retained
    # for an optional GPU measurement but never substituted for CPU memory.
    peak_gpu_memory_bytes: int | None = None

    @property
    def resolved_device_mode(self) -> Literal["cpu", "cuda", "not_run"]:
        """Record the actual device boundary without trusting ambient CUDA."""

        if self.device_mode is not None:
            return self.device_mode
        if self.candidate == GPU_RERANK_CANDIDATE:
            return "cuda" if self.available else "not_run"
        return "cpu"

    def safe_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["device_mode"] = self.resolved_device_mode
        return payload


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    selected: str
    reason: str


@dataclass(frozen=True, slots=True)
class BenchmarkApplication:
    """A verified, explicitly deployable benchmark decision.

    This is intentionally narrower than :class:`CandidateDecision`: Kiwi and
    CUDA-only candidates can be useful measurements, but neither has a
    matching persistent runtime configuration in the Evidence service.
    """

    selected_candidate: str
    retrieval_strategy: RuntimeRetrievalStrategy
    expected_manifest_sha256: str
    benchmark_result_sha256: str
    held_out_approval: HeldOutApproval
    evaluation_law_as_of: date


RetrieveLawIds = Callable[[str, int], Sequence[str]]


def runtime_strategy_for_candidate(candidate: str) -> RuntimeRetrievalStrategy | None:
    """Return the exact runtime strategy represented by a benchmark candidate.

    CPU rerank is the only rerank candidate currently deployable because the
    active profile records a strategy, not a CUDA device contract.  Treating a
    fast GPU-only measurement as a CPU deployment would make the published
    p95 claim misleading, so it is intentionally benchmark-only along with
    the separately implemented Kiwi sparse index.
    """

    if candidate == BASELINE_CANDIDATE:
        return "rrf_lexical"
    if candidate == CPU_RERANK_CANDIDATE:
        return "cross_encoder_rerank"
    if candidate == FIVE_STAGE_CPU_CANDIDATE:
        return "five_stage_cpu"
    return None


def validate_held_out_cases(cases: Iterable[HeldOutCase]) -> tuple[HeldOutCase, ...]:
    """Validate a small fixed held-out suite without retaining its contents."""

    normalized = tuple(cases)
    if not normalized:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_EMPTY")
    seen: set[str] = set()
    for case in normalized:
        if (
            not case.case_id
            or case.case_id in seen
            or not case.query.strip()
            or len(case.query) > 2000
            or not case.expected_law_ids
            or len(case.expected_law_ids) != len(set(case.expected_law_ids))
        ):
            raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_INVALID")
        seen.add(case.case_id)
    return normalized


def parse_held_out_suite(raw: bytes) -> HeldOutSuite:
    """Parse a private V2 suite without accepting self-asserted provenance.

    Independent review is recorded only in the separate, query-free approval
    receipt.  The suite itself has no free-form provenance label that a
    benchmark author could use as its own acceptance evidence.
    """

    suite_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_INVALID") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "suite_id", "cases"}:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_INVALID")
    suite_id = payload["suite_id"]
    raw_cases = payload["cases"]
    if (
        payload["schema_version"] != "2.0"
        or not isinstance(suite_id, str)
        or _LOCAL_ID.fullmatch(suite_id) is None
        or not isinstance(raw_cases, list)
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_INVALID")
    cases: list[HeldOutCase] = []
    for case in raw_cases:
        if not isinstance(case, dict) or set(case) != {"case_id", "query", "expected_law_ids"}:
            raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_INVALID")
        case_id = case["case_id"]
        query = case["query"]
        expected = case["expected_law_ids"]
        if (
            not isinstance(case_id, str)
            or not isinstance(query, str)
            or not isinstance(expected, list)
            or not all(isinstance(value, str) for value in expected)
        ):
            raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_INVALID")
        cases.append(HeldOutCase(case_id, query, tuple(expected)))
    return HeldOutSuite(
        suite_id=suite_id,
        suite_sha256=suite_sha256,
        cases=validate_held_out_cases(cases),
    )


def validate_held_out_approval(*, suite: HeldOutSuite, receipt_raw: bytes) -> HeldOutApproval:
    """Verify a distinct, query-free local approval receipt for exact bytes."""

    receipt_sha256 = hashlib.sha256(receipt_raw).hexdigest()
    try:
        receipt = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID") from exc
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version",
        "approval_id",
        "approved_at",
        "approval_status",
        "suite_id",
        "suite_sha256",
        "case_count",
        "law_as_of",
    }:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID")
    approval_id = receipt["approval_id"]
    approved_at = receipt["approved_at"]
    if (
        receipt["schema_version"] != "law-history-held-out-approval/v2"
        or receipt["approval_status"] != "approved"
        or not isinstance(approval_id, str)
        or _LOCAL_ID.fullmatch(approval_id) is None
        or not isinstance(approved_at, str)
        or not isinstance(receipt["suite_id"], str)
        or not isinstance(receipt["suite_sha256"], str)
        or type(receipt["case_count"]) is not int
        or not isinstance(receipt["law_as_of"], str)
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID")
    try:
        approved_at_value = datetime.fromisoformat(approved_at)
    except ValueError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID") from exc
    if approved_at_value.tzinfo is None or approved_at_value.utcoffset() is None:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID")
    try:
        law_as_of = date.fromisoformat(receipt["law_as_of"])
    except ValueError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID") from exc
    if law_as_of.isoformat() != receipt["law_as_of"]:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID")
    if (
        receipt["suite_id"] != suite.suite_id
        or receipt["suite_sha256"] != suite.suite_sha256
        or receipt["case_count"] != suite.case_count
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_MISMATCH")
    approval = HeldOutApproval(
        suite_id=suite.suite_id,
        suite_sha256=suite.suite_sha256,
        case_count=suite.case_count,
        law_as_of=law_as_of,
        approval_id=approval_id,
        approval_receipt_sha256=receipt_sha256,
    )
    _validate_held_out_approval(approval)
    return approval


def _validate_held_out_approval(approval: HeldOutApproval) -> None:
    if (
        _LOCAL_ID.fullmatch(approval.suite_id) is None
        or _LOCAL_ID.fullmatch(approval.approval_id) is None
        or not _is_sha256(approval.suite_sha256)
        or not _is_sha256(approval.approval_receipt_sha256)
        or approval.case_count < 1
        or type(approval.law_as_of) is not date
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID")


def _percentile95(values: Sequence[float]) -> float:
    if not values:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_EMPTY")
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _valid_memory_bytes(value: object) -> int | None:
    """Return a non-negative byte count without accepting booleans."""

    if type(value) is int and value >= 0:
        return value
    return None


def _windows_working_set_bytes() -> int | None:
    """Read the current process working set without adding a dependency."""

    try:
        import ctypes

        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("page_fault_count", ctypes.c_ulong),
                ("peak_working_set_size", ctypes.c_size_t),
                ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t),
                ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
                ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t),
                ("peak_pagefile_usage", ctypes.c_size_t),
                ("private_usage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        success = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
            ctypes.windll.kernel32.GetCurrentProcess(),  # type: ignore[attr-defined]
            ctypes.byref(counters),
            counters.cb,
        )
        if not success:
            return None
        return _valid_memory_bytes(int(counters.working_set_size))
    except Exception:
        return None


def _process_resident_memory_bytes() -> int | None:
    """Read current process RSS, including native allocations, or fail closed.

    ``tracemalloc`` reports only Python-tracked allocations and misses the
    native tensors/model buffers that matter for a deployable reranker.  The
    benchmark deliberately requires an OS-level resident-set probe instead.
    ``psutil`` is used when installed through the existing local RAG stack;
    Linux and Windows retain small dependency-free fallbacks.
    """

    try:
        import psutil

        rss = _valid_memory_bytes(int(psutil.Process().memory_info().rss))
        if rss is not None:
            return rss
    except Exception:
        pass

    if sys.platform.startswith("linux"):
        try:
            with open("/proc/self/statm", encoding="ascii") as handle:
                fields = handle.read().split()
            if len(fields) < 2:
                return None
            pages = int(fields[1])
            page_size = int(os.sysconf("SC_PAGE_SIZE"))
            return _valid_memory_bytes(pages * page_size)
        except (OSError, ValueError):
            return None
    if sys.platform == "win32":
        return _windows_working_set_bytes()
    return None


def _prepare_cuda_peak_memory_probe() -> bool:
    """Reset CUDA's allocation high-water mark for one explicit GPU run."""

    try:
        import torch

        if not torch.cuda.is_available():
            return False
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
    except Exception:
        return False
    return True


def _cuda_peak_memory_bytes() -> int | None:
    """Return this run's CUDA tensor-allocation peak, never host RSS."""

    try:
        import torch

        if not torch.cuda.is_available():
            return None
        torch.cuda.synchronize()
        return _valid_memory_bytes(int(torch.cuda.max_memory_allocated()))
    except Exception:
        return None


def measure_candidate(
    *,
    candidate: str,
    cases: Iterable[HeldOutCase],
    retrieve_law_ids: RetrieveLawIds | None,
    device_mode: Literal["cpu", "cuda"] | None = None,
) -> CandidateMetrics:
    """Measure strict recall, latency, and native-inclusive local memory.

    A candidate is unavailable if its host RSS cannot be measured.  An actual
    CUDA candidate additionally needs a separately reset/read GPU allocation
    high-water mark, so a host-only number can never masquerade as GPU memory.
    """

    normalized = validate_held_out_cases(cases)
    if retrieve_law_ids is None:
        return CandidateMetrics(candidate, False, None, None, None, None, len(normalized))
    measured_device = device_mode or ("cuda" if candidate == GPU_RERANK_CANDIDATE else "cpu")
    if measured_device == "cuda" and not _prepare_cuda_peak_memory_probe():
        return CandidateMetrics(
            candidate,
            False,
            None,
            None,
            None,
            None,
            len(normalized),
            device_mode="cuda",
        )
    initial_rss = _process_resident_memory_bytes()
    if initial_rss is None:
        return CandidateMetrics(
            candidate,
            False,
            None,
            None,
            None,
            None,
            len(normalized),
            device_mode=measured_device,
        )
    elapsed_ms: list[float] = []
    recall5: list[float] = []
    recall10: list[float] = []
    peak_rss = initial_rss
    try:
        for case in normalized:
            started = time.perf_counter_ns()
            retrieved = tuple(retrieve_law_ids(case.query, 10))
            elapsed_ms.append((time.perf_counter_ns() - started) / 1_000_000)
            current_rss = _process_resident_memory_bytes()
            if current_rss is None:
                raise LawHistoryBenchmarkError("LAW_HISTORY_PROCESS_MEMORY_UNAVAILABLE")
            peak_rss = max(peak_rss, current_rss)
            expected = set(case.expected_law_ids)
            # "Strict" means every independently reviewed target law for a
            # case must be present at the cutoff.  Partial target overlap is
            # useful diagnostics but cannot pass the published adoption gate.
            recall5.append(float(expected <= set(retrieved[:5])))
            recall10.append(float(expected <= set(retrieved[:10])))
        peak_gpu = _cuda_peak_memory_bytes() if measured_device == "cuda" else None
        if measured_device == "cuda" and peak_gpu is None:
            raise LawHistoryBenchmarkError("LAW_HISTORY_GPU_MEMORY_UNAVAILABLE")
    except Exception:
        return CandidateMetrics(
            candidate,
            False,
            None,
            None,
            None,
            None,
            len(normalized),
            device_mode=measured_device,
        )
    return CandidateMetrics(
        candidate=candidate,
        available=True,
        strict_recall_at_5=round(statistics.fmean(recall5), 8),
        strict_recall_at_10=round(statistics.fmean(recall10), 8),
        p95_ms=round(_percentile95(elapsed_ms), 6),
        peak_memory_bytes=peak_rss,
        case_count=len(normalized),
        device_mode=measured_device,
        peak_gpu_memory_bytes=peak_gpu,
    )


def choose_candidate(
    *,
    baseline: CandidateMetrics,
    candidates: Iterable[CandidateMetrics],
    latency_exempt: frozenset[str] = LATENCY_EXEMPT_CANDIDATES,
) -> CandidateDecision:
    """Apply the published non-regression and deterministic tie-break rules.

    ``latency_exempt`` candidates skip only the p95 gate; their recall must
    still be at least the baseline's and their latency must be measured.
    """

    if (
        not baseline.available
        or baseline.strict_recall_at_5 is None
        or baseline.strict_recall_at_10 is None
        or baseline.p95_ms is None
        or baseline.peak_memory_bytes is None
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BASELINE_BENCHMARK_UNAVAILABLE")
    eligible: list[CandidateMetrics] = []
    for candidate in candidates:
        if candidate.candidate == baseline.candidate or not candidate.available:
            continue
        if (
            candidate.strict_recall_at_5 is None
            or candidate.strict_recall_at_10 is None
            or candidate.p95_ms is None
            or candidate.peak_memory_bytes is None
        ):
            continue
        if candidate.strict_recall_at_5 < baseline.strict_recall_at_5:
            continue
        if candidate.strict_recall_at_10 < baseline.strict_recall_at_10:
            continue
        if candidate.candidate not in latency_exempt and candidate.p95_ms > baseline.p95_ms * 1.10:
            continue
        eligible.append(candidate)
    if not eligible:
        return CandidateDecision(baseline.candidate, "no_candidate_passed_non_regression")

    # Better strict recall wins first.  With identical quality, use the
    # required latency then memory tie-break; a total tie retains baseline.
    best = sorted(
        eligible,
        key=lambda value: (
            -cast_float(value.strict_recall_at_5),
            -cast_float(value.strict_recall_at_10),
            cast_float(value.p95_ms),
            cast_int(value.peak_memory_bytes),
            value.candidate,
        ),
    )[0]
    baseline_tuple = (
        baseline.strict_recall_at_5,
        baseline.strict_recall_at_10,
        baseline.p95_ms,
        baseline.peak_memory_bytes,
    )
    best_tuple = (
        best.strict_recall_at_5,
        best.strict_recall_at_10,
        best.p95_ms,
        best.peak_memory_bytes,
    )
    if best_tuple == baseline_tuple:
        return CandidateDecision(baseline.candidate, "all_metrics_tied_keep_baseline")
    return CandidateDecision(best.candidate, "passed_non_regression_and_tie_break")


def choose_deployable_candidate(
    *,
    baseline: CandidateMetrics,
    cpu_rerank: CandidateMetrics,
    five_stage: CandidateMetrics | None = None,
) -> CandidateDecision:
    """Make the promotion decision from the executable service modes.

    Kiwi and CUDA measurements remain in the aggregate as diagnostics, but
    their local index/device setup has no matching persistent Evidence-service
    configuration.  They also run after the CPU candidates in this one local
    benchmark process, so their RSS must not be presented as an unbiased
    all-candidate promotion tie-break.  Only the baseline, the CPU reranker,
    and the five-stage CPU pipeline represent deployable strategies and may
    influence an apply result.
    """

    if baseline.candidate != BASELINE_CANDIDATE or cpu_rerank.candidate != CPU_RERANK_CANDIDATE:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_CANDIDATES_INVALID")
    deployable: list[CandidateMetrics] = [cpu_rerank]
    if five_stage is not None:
        if five_stage.candidate != FIVE_STAGE_CPU_CANDIDATE:
            raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_CANDIDATES_INVALID")
        deployable.append(five_stage)
    return choose_candidate(baseline=baseline, candidates=tuple(deployable))


def benchmark_result_payload(
    *,
    metrics: Sequence[CandidateMetrics],
    decision: CandidateDecision,
    active_manifest_sha256: str,
    held_out_approval: HeldOutApproval,
    generated_at: str,
) -> dict[str, object]:
    """Create a query-free aggregate bound to a reviewed held-out receipt."""

    if not _is_sha256(active_manifest_sha256):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_MANIFEST_DIGEST_INVALID")
    _validate_held_out_approval(held_out_approval)
    by_candidate = {value.candidate: value for value in metrics}
    if len(by_candidate) != len(metrics) or set(by_candidate) != BENCHMARK_CANDIDATES:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_CANDIDATES_INVALID")
    _validate_benchmark_candidate_set(by_candidate)
    if by_candidate[BASELINE_CANDIDATE].case_count != held_out_approval.case_count:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_MISMATCH")
    baseline = by_candidate[BASELINE_CANDIDATE]
    expected = choose_deployable_candidate(
        baseline=baseline,
        cpu_rerank=by_candidate[CPU_RERANK_CANDIDATE],
        five_stage=by_candidate[FIVE_STAGE_CPU_CANDIDATE],
    )
    if decision != expected:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_DECISION_INVALID")
    if not isinstance(generated_at, str) or not generated_at:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_TIMESTAMP_INVALID")
    return {
        # V4 rejected the earlier tracemalloc-only aggregates at the explicit
        # apply boundary.  V5 adds the five-stage CPU candidate to the fixed
        # candidate set, so a V4 aggregate can no longer be applied either.
        "schema_version": BENCHMARK_RESULT_SCHEMA_VERSION,
        "profile": "functional-uat-law-history",
        "generated_at": generated_at,
        "active_manifest_sha256": active_manifest_sha256,
        "held_out_suite": held_out_approval.result_dict(),
        "evaluation_law_as_of": held_out_approval.law_as_of.isoformat(),
        "metrics": [value.safe_dict() for value in metrics],
        "decision": {
            "selected": decision.selected,
            "reason": decision.reason,
            # This field is informational only.  An explicit apply parses the
            # selected candidate again and rejects a non-deployable result.
            "runtime_strategy": runtime_strategy_for_candidate(decision.selected),
        },
    }


def benchmark_application_from_bytes(
    raw: bytes, *, held_out_approval: HeldOutApproval
) -> BenchmarkApplication:
    """Validate an aggregate result before a separate active-profile apply.

    The parser checks the candidate set, recomputes the deterministic
    selection rule, and requires the freshly verified query-free approval
    receipt for the exact V2 suite.  It accepts no held-out questions,
    expected answers, passages, model output, or file paths, so the explicit
    apply path cannot import clone Goldset data by accident.
    """

    _validate_held_out_approval(held_out_approval)
    digest = hashlib.sha256(raw).hexdigest()
    try:
        import json

        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "profile",
        "generated_at",
        "active_manifest_sha256",
        "held_out_suite",
        "evaluation_law_as_of",
        "metrics",
        "decision",
    }:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    if (
        payload["schema_version"] != BENCHMARK_RESULT_SCHEMA_VERSION
        or payload["profile"] != "functional-uat-law-history"
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    generated_at = payload["generated_at"]
    if not isinstance(generated_at, str):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    try:
        parsed_generated_at = datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID") from exc
    if parsed_generated_at.tzinfo is None or parsed_generated_at.utcoffset() is None:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    manifest_digest = payload["active_manifest_sha256"]
    if not isinstance(manifest_digest, str) or not _is_sha256(manifest_digest):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    if payload["held_out_suite"] != held_out_approval.result_dict():
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_MISMATCH")
    evaluation_law_as_of = payload["evaluation_law_as_of"]
    if not isinstance(evaluation_law_as_of, str):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    try:
        parsed_evaluation_law_as_of = date.fromisoformat(evaluation_law_as_of)
    except ValueError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID") from exc
    if (
        parsed_evaluation_law_as_of.isoformat() != evaluation_law_as_of
        or parsed_evaluation_law_as_of != held_out_approval.law_as_of
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_MISMATCH")
    raw_metrics = payload["metrics"]
    if not isinstance(raw_metrics, list):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    metrics = tuple(_candidate_metrics_from_dict(value) for value in raw_metrics)
    by_candidate = {value.candidate: value for value in metrics}
    if len(by_candidate) != len(metrics) or set(by_candidate) != BENCHMARK_CANDIDATES:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    try:
        _validate_benchmark_candidate_set(by_candidate)
    except LawHistoryBenchmarkError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID") from exc
    if by_candidate[BASELINE_CANDIDATE].case_count != held_out_approval.case_count:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_MISMATCH")
    try:
        expected = choose_deployable_candidate(
            baseline=by_candidate[BASELINE_CANDIDATE],
            cpu_rerank=by_candidate[CPU_RERANK_CANDIDATE],
            five_stage=by_candidate[FIVE_STAGE_CPU_CANDIDATE],
        )
    except LawHistoryBenchmarkError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID") from exc
    decision = payload["decision"]
    if not isinstance(decision, dict) or set(decision) != {
        "selected",
        "reason",
        "runtime_strategy",
    }:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    if decision.get("selected") != expected.selected or decision.get("reason") != expected.reason:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    strategy = runtime_strategy_for_candidate(expected.selected)
    if decision.get("runtime_strategy") != strategy:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    if strategy is None:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_CANDIDATE_NOT_DEPLOYABLE")
    return BenchmarkApplication(
        selected_candidate=expected.selected,
        retrieval_strategy=strategy,
        expected_manifest_sha256=manifest_digest,
        benchmark_result_sha256=digest,
        held_out_approval=held_out_approval,
        evaluation_law_as_of=parsed_evaluation_law_as_of,
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _validate_benchmark_candidate_set(
    by_candidate: dict[str, CandidateMetrics],
) -> None:
    """Ensure every candidate used the same held-out suite and encoder rules."""

    if len({metric.case_count for metric in by_candidate.values()}) != 1:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_CASE_COUNT_MISMATCH")
    for candidate, metric in by_candidate.items():
        device_mode = metric.resolved_device_mode
        if candidate == GPU_RERANK_CANDIDATE:
            if device_mode not in {"cuda", "not_run"} or (
                metric.available and device_mode != "cuda"
            ):
                raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_DEVICE_MODE_INVALID")
            if metric.available != (metric.peak_gpu_memory_bytes is not None):
                raise LawHistoryBenchmarkError("LAW_HISTORY_GPU_MEMORY_INVALID")
        elif device_mode != "cpu":
            raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_DEVICE_MODE_INVALID")
        elif metric.peak_gpu_memory_bytes is not None:
            raise LawHistoryBenchmarkError("LAW_HISTORY_GPU_MEMORY_INVALID")
        if candidate == KIWI_CANDIDATE:
            if metric.available and metric.encoder_fingerprint is None:
                raise LawHistoryBenchmarkError("LAW_HISTORY_KIWI_FINGERPRINT_MISSING")
            if not metric.available and metric.encoder_fingerprint is not None:
                raise LawHistoryBenchmarkError("LAW_HISTORY_KIWI_FINGERPRINT_INVALID")
        elif metric.encoder_fingerprint is not None:
            raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_FINGERPRINT_INVALID")


def _candidate_metrics_from_dict(value: object) -> CandidateMetrics:
    if not isinstance(value, dict) or set(value) != {
        "candidate",
        "available",
        "strict_recall_at_5",
        "strict_recall_at_10",
        "p95_ms",
        "peak_memory_bytes",
        "peak_gpu_memory_bytes",
        "case_count",
        "encoder_fingerprint",
        "device_mode",
    }:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    candidate = value["candidate"]
    available = value["available"]
    fingerprint = value["encoder_fingerprint"]
    device_mode = value["device_mode"]
    gpu_peak = value["peak_gpu_memory_bytes"]
    if (
        not isinstance(candidate, str)
        or not isinstance(available, bool)
        or device_mode not in {"cpu", "cuda", "not_run"}
        or (gpu_peak is not None and _valid_memory_bytes(gpu_peak) is None)
        or (
            fingerprint is not None
            and (not isinstance(fingerprint, str) or not _is_sha256(fingerprint))
        )
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    numeric_fields = (
        "strict_recall_at_5",
        "strict_recall_at_10",
        "p95_ms",
        "peak_memory_bytes",
    )
    values: dict[str, float | int | None] = {}
    for field in numeric_fields:
        raw_value = value[field]
        if raw_value is None:
            values[field] = None
        elif field == "peak_memory_bytes" and type(raw_value) is int and raw_value >= 0:
            values[field] = raw_value
        elif (
            field != "peak_memory_bytes"
            and isinstance(raw_value, (int, float))
            and not isinstance(raw_value, bool)
        ):
            values[field] = float(raw_value)
        else:
            raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    case_count = value["case_count"]
    if type(case_count) is not int or case_count < 1:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    if available != all(values[field] is not None for field in numeric_fields):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    if available:
        recall5 = cast(float, values["strict_recall_at_5"])
        recall10 = cast(float, values["strict_recall_at_10"])
        p95_ms = cast(float, values["p95_ms"])
        if (
            not math.isfinite(recall5)
            or not math.isfinite(recall10)
            or not math.isfinite(p95_ms)
            or not 0 <= recall5 <= 1
            or not 0 <= recall10 <= 1
            or p95_ms < 0
        ):
            raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID")
    return CandidateMetrics(
        candidate=candidate,
        available=available,
        strict_recall_at_5=cast(float | None, values["strict_recall_at_5"]),
        strict_recall_at_10=cast(float | None, values["strict_recall_at_10"]),
        p95_ms=cast(float | None, values["p95_ms"]),
        peak_memory_bytes=cast(int | None, values["peak_memory_bytes"]),
        case_count=case_count,
        encoder_fingerprint=fingerprint,
        device_mode=cast(Literal["cpu", "cuda", "not_run"], device_mode),
        peak_gpu_memory_bytes=cast(int | None, gpu_peak),
    )


def cast_float(value: float | None) -> float:
    if value is None:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_INVALID")
    return value


def cast_int(value: int | None) -> int:
    if value is None:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_INVALID")
    return value


class KiwiSparseIndex:
    """Separate in-process Korean sparse index; it never reuses Qdrant BM25.

    The index is intended only for the benchmark process and can be rebuilt
    from the local temporal corpus.  It tokenizes with the exact product
    encoder (``src.embed.bm25``), so the diagnostic measures the product
    tokenizer under a plain in-memory BM25 rather than a second tokenizer.
    Its fingerprint binds the encoder rules and the installed Kiwi release,
    so a result cannot be compared against a different sparse encoder
    accidentally.
    """

    def __init__(self) -> None:
        from src.embed import bm25

        try:
            self.encoder_fingerprint = bm25.encoder_fingerprint()
        except bm25.KiwiUnavailable as exc:
            raise LawHistoryBenchmarkError("LAW_HISTORY_KIWI_UNAVAILABLE") from exc
        self._tokenize = bm25.tokenize
        self._documents: dict[str, Counter[str]] = {}
        self._document_frequency: Counter[str] = Counter()

    def _tokens(self, text: str) -> tuple[str, ...]:
        try:
            return tuple(self._tokenize(text))
        except RuntimeError as exc:
            raise LawHistoryBenchmarkError("LAW_HISTORY_KIWI_UNAVAILABLE") from exc

    def build(self, documents: Iterable[tuple[str, str]]) -> None:
        self._documents.clear()
        self._document_frequency.clear()
        for source_id, text in documents:
            if not source_id or source_id in self._documents:
                raise LawHistoryBenchmarkError("LAW_HISTORY_KIWI_DOCUMENT_INVALID")
            terms = Counter(self._tokens(text))
            self._documents[source_id] = terms
            self._document_frequency.update(terms)
        if not self._documents:
            raise LawHistoryBenchmarkError("LAW_HISTORY_KIWI_DOCUMENT_EMPTY")

    def search(self, query: str, limit: int) -> tuple[str, ...]:
        terms = Counter(self._tokens(query))
        count = len(self._documents)
        scored: list[tuple[float, str]] = []
        for source_id, document in self._documents.items():
            score = sum(
                query_tf
                * document_tf
                * math.log((count + 1) / (self._document_frequency[term] + 1))
                for term, query_tf in terms.items()
                for document_tf in (document.get(term, 0),)
            )
            if score > 0:
                scored.append((score, source_id))
        return tuple(source_id for _score, source_id in sorted(scored, reverse=True)[:limit])


__all__ = [
    "BASELINE_CANDIDATE",
    "BENCHMARK_CANDIDATES",
    "BENCHMARK_RESULT_SCHEMA_VERSION",
    "CPU_RERANK_CANDIDATE",
    "FIVE_STAGE_CPU_CANDIDATE",
    "LATENCY_EXEMPT_CANDIDATES",
    "CandidateDecision",
    "CandidateMetrics",
    "GPU_RERANK_CANDIDATE",
    "HeldOutApproval",
    "HeldOutCase",
    "HeldOutSuite",
    "KIWI_CANDIDATE",
    "KiwiSparseIndex",
    "BenchmarkApplication",
    "LawHistoryBenchmarkError",
    "RuntimeRetrievalStrategy",
    "benchmark_application_from_bytes",
    "benchmark_result_payload",
    "choose_candidate",
    "choose_deployable_candidate",
    "measure_candidate",
    "parse_held_out_suite",
    "runtime_strategy_for_candidate",
    "validate_held_out_approval",
    "validate_held_out_cases",
]
