"""Run a local, held-out retrieval benchmark for the law-history profile.

The V2 input set and its separate query-free approval receipt must both live
below ignored ``_local/data/law-history-heldout``.  The V2 receipt binds suite
ID, exact suite bytes, case count, and the active legal evaluation date; it is
required again for explicit apply.  The command never accepts the cloned
repository's Goldset, performs network/model downloads, or writes query text,
expected answers, passages, or provider output to its aggregate result.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
for import_root in (ROOT, LAW_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from src.law_history import (  # noqa: E402
    LAW_HISTORY_BASELINE_AS_OF,
    FunctionalLawHistoryEvidenceBackend,
    LawHistoryError,
    validate_law_history_state,
    verify_law_history_store,
)
from src.law_history_benchmark import (  # noqa: E402
    BASELINE_CANDIDATE,
    CPU_RERANK_CANDIDATE,
    GPU_RERANK_CANDIDATE,
    KIWI_CANDIDATE,
    CandidateDecision,
    CandidateMetrics,
    HeldOutApproval,
    HeldOutSuite,
    KiwiSparseIndex,
    LawHistoryBenchmarkError,
    benchmark_application_from_bytes,
    benchmark_result_payload,
    choose_deployable_candidate,
    measure_candidate,
    parse_held_out_suite,
    validate_held_out_approval,
)
from src.local_profiles import local_root  # noqa: E402
from src.local_profiles import functional_uat_law_history_cuda_opt_in  # noqa: E402


def _safe_held_out_path(value: str) -> Path:
    candidate = Path(value)
    root = local_root() / "data" / "law-history-heldout"
    if root.is_symlink():
        raise argparse.ArgumentTypeError("held-out root is unavailable")
    if not candidate.is_absolute():
        raise argparse.ArgumentTypeError("held-out path must be absolute")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "held-out path must stay under _local/data/law-history-heldout"
        ) from exc
    except OSError as exc:
        raise argparse.ArgumentTypeError("held-out file is unavailable") from exc
    if candidate.suffix.casefold() != ".json" or not candidate.is_file() or candidate.is_symlink():
        raise argparse.ArgumentTypeError("held-out file is unavailable")
    return candidate


def _safe_benchmark_result_path(value: str) -> Path:
    """Accept only a prior aggregate result from the ignored local state."""

    candidate = Path(value)
    root = local_root() / "state" / "law-history-benchmarks"
    if root.is_symlink():
        raise argparse.ArgumentTypeError("benchmark result root is unavailable")
    if not candidate.is_absolute():
        raise argparse.ArgumentTypeError("benchmark result path must be absolute")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "benchmark result must stay under _local/state/law-history-benchmarks"
        ) from exc
    except OSError as exc:
        raise argparse.ArgumentTypeError("benchmark result is unavailable") from exc
    if candidate.suffix.casefold() != ".json" or not candidate.is_file() or candidate.is_symlink():
        raise argparse.ArgumentTypeError("benchmark result is unavailable")
    return candidate


def _safe_approval_receipt_path(value: str) -> Path:
    """Require a query-free approval receipt in its own ignored local root."""

    candidate = Path(value)
    root = local_root() / "data" / "law-history-heldout" / "approvals"
    if root.is_symlink():
        raise argparse.ArgumentTypeError("held-out approval root is unavailable")
    if not candidate.is_absolute():
        raise argparse.ArgumentTypeError("held-out approval path must be absolute")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "held-out approval must stay under _local/data/law-history-heldout/approvals"
        ) from exc
    except OSError as exc:
        raise argparse.ArgumentTypeError("held-out approval is unavailable") from exc
    if candidate.suffix.casefold() != ".json" or not candidate.is_file() or candidate.is_symlink():
        raise argparse.ArgumentTypeError("held-out approval is unavailable")
    return candidate


def _load_held_out(path: Path) -> HeldOutSuite:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_INVALID") from exc
    return parse_held_out_suite(raw)


def _load_held_out_approval(path: Path, *, suite: HeldOutSuite) -> HeldOutApproval:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_HELD_OUT_APPROVAL_INVALID") from exc
    return validate_held_out_approval(suite=suite, receipt_raw=raw)


def _active_coverage_through(manifest: object) -> date:
    """Parse the only evaluation date that an active result may bind."""

    if not isinstance(manifest, dict):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
    raw = manifest.get("coverage_through")
    if not isinstance(raw, str):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE") from exc
    if parsed.isoformat() != raw:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
    return parsed


class _LawIdRetriever:
    """Adapter that records whether the optional reranker really ran."""

    def __init__(
        self,
        backend: FunctionalLawHistoryEvidenceBackend,
        *,
        as_of: date,
        require_rerank: bool = False,
    ) -> None:
        self._backend = backend
        self._as_of = as_of
        self._require_rerank = require_rerank
        self.rerank_observed = False

    def __call__(self, query: str, limit: int) -> tuple[str, ...]:
        matches = self._backend.search_law_articles(query, limit, self._as_of)
        if (
            self._require_rerank
            and matches
            and not all("rerank" in match.retrieval_methods for match in matches)
        ):
            raise LawHistoryBenchmarkError("LAW_HISTORY_RERANK_UNAVAILABLE")
        if self._require_rerank and matches:
            self.rerank_observed = True
        return tuple(str(match.row["law_id"]) for match in matches)


def _law_id_retriever(
    backend: FunctionalLawHistoryEvidenceBackend,
    *,
    as_of: date,
    require_rerank: bool = False,
) -> _LawIdRetriever:
    return _LawIdRetriever(backend, as_of=as_of, require_rerank=require_rerank)


def _kiwi_retriever(*, as_of: date) -> tuple[KiwiSparseIndex, Any]:
    """Build a distinct Kiwi index from the same date-valid corpus.

    The comparison must not let Kiwi see future or superseded legal text that
    the temporal Qdrant baseline has already filtered out.  This index is
    separate from Qdrant's sparse vector, but it is constructed only from
    the exact half-open date slice being benchmarked.
    """

    from src.db.postgres import connect

    index = KiwiSparseIndex()
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT concat(a.law_id, ':', a.id), a.content
            FROM law_articles a
            JOIN law_version_sources s ON s.law_id=a.law_id AND s.mst=a.version_mst
            WHERE a.valid_from <= %s AND (a.valid_to IS NULL OR a.valid_to > %s)
              AND NOT EXISTS (SELECT 1 FROM law_articles c WHERE c.parent_article_pk=a.id)
            ORDER BY a.id
            """,
            (as_of, as_of),
        )
        documents = tuple((str(row[0]), str(row[1])) for row in cur.fetchall())
    index.build(documents)

    def retrieve(query: str, limit: int) -> tuple[str, ...]:
        return tuple(source_id.split(":", 1)[0] for source_id in index.search(query, limit))

    return index, retrieve


def _write_metrics(
    *,
    metrics: list[CandidateMetrics],
    decision: object,
    active_manifest_sha256: str,
    held_out_approval: HeldOutApproval,
) -> Path:
    root = local_root() / "state" / "law-history-benchmarks"
    if root.is_symlink():
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_OUTPUT_ROOT_UNSAFE")
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}.json"
    if output.exists() or output.is_symlink():
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_OUTPUT_EXISTS")
    if not isinstance(decision, CandidateDecision):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_DECISION_INVALID")
    payload = benchmark_result_payload(
        metrics=metrics,
        decision=decision,
        active_manifest_sha256=active_manifest_sha256,
        held_out_approval=held_out_approval,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            handle.write("\n")
    except OSError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_OUTPUT_WRITE_FAILED") from exc
    return output


def _apply_result(path: Path, *, held_out_approval: HeldOutApproval) -> str:
    """Perform the separate, fail-closed active manifest binding.

    The benchmark command itself never changes serving behavior.  A reviewed
    aggregate must be supplied explicitly, it must still bind to the active
    manifest measured during the run, and the profile module is the only
    code allowed to mutate that marker.
    """

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_RESULT_INVALID") from exc
    application = benchmark_application_from_bytes(raw, held_out_approval=held_out_approval)
    try:
        manifest, active_manifest_sha256 = validate_law_history_state()
        coverage_through = _active_coverage_through(manifest)
    except (LawHistoryBenchmarkError, LawHistoryError) as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE") from exc
    if (
        active_manifest_sha256 != application.expected_manifest_sha256
        or coverage_through != application.evaluation_law_as_of
    ):
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
    # Import at the mutation boundary so a benchmark-only invocation cannot
    # reach an active-manifest writer accidentally.
    try:
        from src.law_history import bind_law_history_retrieval_strategy
    except ImportError as exc:
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_APPLY_UNAVAILABLE") from exc

    try:
        return bind_law_history_retrieval_strategy(
            strategy=application.retrieval_strategy,
            expected_manifest_sha256=application.expected_manifest_sha256,
            benchmark_result_sha256=application.benchmark_result_sha256,
        )
    except Exception as exc:
        # Do not expose profile paths, raw provider errors, or manifest bytes
        # from this local operator command.
        if isinstance(exc, LawHistoryError):
            raise LawHistoryBenchmarkError(str(exc)) from exc
        raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_APPLY_FAILED") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--held-out", type=_safe_held_out_path, help="private V2 held-out suite")
    parser.add_argument(
        "--approval-receipt",
        type=_safe_approval_receipt_path,
        help="query-free local approval receipt for that exact suite",
    )
    parser.add_argument(
        "--apply-result",
        type=_safe_benchmark_result_path,
        help="prior aggregate result; revalidates the suite and approval receipt",
    )
    parser.add_argument(
        "--allow-cuda",
        action="store_true",
        help="measure the optional GPU reranker inside an explicit local-only scope",
    )
    args = parser.parse_args(argv)
    if args.held_out is None or args.approval_receipt is None:
        parser.error("--held-out and --approval-receipt are required for measurement and apply")
    try:
        suite = _load_held_out(args.held_out)
        held_out_approval = _load_held_out_approval(args.approval_receipt, suite=suite)
        if args.apply_result is not None:
            _apply_result(args.apply_result, held_out_approval=held_out_approval)
            print("law-history benchmark retrieval strategy applied")
            return 0
        # The held-out measurement must run against the same complete
        # temporal projection that an explicit apply is allowed to bind.
        # Count-only readiness is insufficient: a swapped Qdrant payload can
        # preserve counts while changing the benchmark corpus.
        verified = verify_law_history_store()
        verified_digest = verified.get("manifest_sha256")
        verified_projection = verified.get("temporal_store_projection_sha256")
        manifest, _digest = validate_law_history_state()
        if (
            not isinstance(verified_digest, str)
            or not isinstance(verified_projection, str)
            or _digest != verified_digest
            or manifest.get("temporal_store_projection_sha256") != verified_projection
        ):
            raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_MANIFEST_STALE")
        cases = suite.cases
        coverage_through = _active_coverage_through(manifest)
        evaluation_as_of = held_out_approval.law_as_of
        if evaluation_as_of != coverage_through:
            raise LawHistoryBenchmarkError("LAW_HISTORY_BENCHMARK_EVALUATION_AS_OF_MISMATCH")
        # Baseline and CPU rerank are the two deployable modes.  They run in
        # one local process intentionally: the CPU figure is the serving
        # working set for base retrieval plus its reranker, not a normalized
        # cross-device comparison with the later diagnostic candidates.
        baseline_backend = FunctionalLawHistoryEvidenceBackend(
            coverage_from=LAW_HISTORY_BASELINE_AS_OF,
            coverage_through=coverage_through,
            use_rerank=False,
        )
        baseline = measure_candidate(
            candidate=BASELINE_CANDIDATE,
            cases=cases,
            retrieve_law_ids=_law_id_retriever(baseline_backend, as_of=evaluation_as_of),
        )
        rerank_backend = FunctionalLawHistoryEvidenceBackend(
            coverage_from=LAW_HISTORY_BASELINE_AS_OF,
            coverage_through=coverage_through,
            use_rerank=True,
        )
        rerank_retriever = _law_id_retriever(
            rerank_backend, as_of=evaluation_as_of, require_rerank=True
        )
        rerank = measure_candidate(
            candidate=CPU_RERANK_CANDIDATE,
            cases=cases,
            retrieve_law_ids=rerank_retriever,
        )
        if rerank.available and not rerank_retriever.rerank_observed:
            rerank = CandidateMetrics(
                CPU_RERANK_CANDIDATE, False, None, None, None, None, len(cases)
            )
        try:
            kiwi, kiwi_retrieve = _kiwi_retriever(as_of=evaluation_as_of)
            kiwi_metrics = measure_candidate(
                candidate=KIWI_CANDIDATE,
                cases=cases,
                retrieve_law_ids=kiwi_retrieve,
            )
            kiwi_metrics = CandidateMetrics(
                candidate=kiwi_metrics.candidate,
                available=kiwi_metrics.available,
                strict_recall_at_5=kiwi_metrics.strict_recall_at_5,
                strict_recall_at_10=kiwi_metrics.strict_recall_at_10,
                p95_ms=kiwi_metrics.p95_ms,
                peak_memory_bytes=kiwi_metrics.peak_memory_bytes,
                case_count=kiwi_metrics.case_count,
                encoder_fingerprint=kiwi.encoder_fingerprint,
                peak_gpu_memory_bytes=kiwi_metrics.peak_gpu_memory_bytes,
            )
        except LawHistoryBenchmarkError:
            kiwi_metrics = measure_candidate(
                candidate=KIWI_CANDIDATE, cases=cases, retrieve_law_ids=None
            )
        # GPU stays opt-in and process-local.  A visible CUDA device alone
        # never changes a normal measurement or service into a GPU run.
        gpu_retriever: _LawIdRetriever | None = None
        gpu_permitted = False
        if bool(args.allow_cuda):
            try:
                import torch

                gpu_permitted = bool(torch.cuda.is_available())
            except Exception:
                gpu_permitted = False
        if gpu_permitted:
            with functional_uat_law_history_cuda_opt_in():
                gpu_retriever = _law_id_retriever(
                    FunctionalLawHistoryEvidenceBackend(
                        coverage_from=LAW_HISTORY_BASELINE_AS_OF,
                        coverage_through=coverage_through,
                        use_rerank=True,
                        rerank_device="cuda",
                    ),
                    as_of=evaluation_as_of,
                    require_rerank=True,
                )
                gpu_metrics = measure_candidate(
                    candidate=GPU_RERANK_CANDIDATE,
                    cases=cases,
                    retrieve_law_ids=gpu_retriever,
                    device_mode="cuda",
                )
            # Retain the actual attempted device even when model inference
            # falls back to unavailable; otherwise a failed CUDA candidate
            # could be mistaken for a deliberately skipped one.
            gpu_metrics = CandidateMetrics(
                candidate=gpu_metrics.candidate,
                available=gpu_metrics.available,
                strict_recall_at_5=gpu_metrics.strict_recall_at_5,
                strict_recall_at_10=gpu_metrics.strict_recall_at_10,
                p95_ms=gpu_metrics.p95_ms,
                peak_memory_bytes=gpu_metrics.peak_memory_bytes,
                case_count=gpu_metrics.case_count,
                encoder_fingerprint=gpu_metrics.encoder_fingerprint,
                device_mode="cuda",
                peak_gpu_memory_bytes=gpu_metrics.peak_gpu_memory_bytes,
            )
        else:
            gpu_metrics = measure_candidate(
                candidate=GPU_RERANK_CANDIDATE,
                cases=cases,
                retrieve_law_ids=None,
            )
        if (
            gpu_metrics.available
            and gpu_retriever is not None
            and not gpu_retriever.rerank_observed
        ):
            gpu_metrics = CandidateMetrics(
                GPU_RERANK_CANDIDATE,
                False,
                None,
                None,
                None,
                None,
                len(cases),
                device_mode="cuda",
            )
        metrics = [baseline, rerank, kiwi_metrics, gpu_metrics]
        decision = choose_deployable_candidate(baseline=baseline, cpu_rerank=rerank)
        path = _write_metrics(
            metrics=metrics,
            decision=decision,
            active_manifest_sha256=_digest,
            held_out_approval=held_out_approval,
        )
        print(f"law-history benchmark aggregate written: {path}")
        return 0
    except (LawHistoryBenchmarkError, LawHistoryError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
