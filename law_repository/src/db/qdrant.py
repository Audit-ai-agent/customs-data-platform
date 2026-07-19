"""Qdrant 현행 인덱스 — customs_law_current (설계 §4.2).

named dense(bge-m3 1024, cosine) + sparse bm25(modifier=idf).
융합/rerank 는 저장소가 하지 않음 — arm 별 {article_pk, score, text} 만 반환.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Lock, Timer
from typing import Any

from qdrant_client import QdrantClient, models

from src.config import settings
from src.deadline import DeadlineExceeded, RequestDeadline

COLLECTION = "customs_law_current"

DENSE_VECTOR = "dense"  # bge-m3 dense → "Vector 검색 근거"
BM25_VECTOR = "bm25"  # Qdrant native sparse(idf) → "BM25 검색 근거"

# 필터/표시용 payload 인덱스 (진실은 PG). 설계 §4.2 표.
_PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "article_pk": models.PayloadSchemaType.INTEGER,
    "law_id": models.PayloadSchemaType.KEYWORD,
    "law_name": models.PayloadSchemaType.KEYWORD,
    "hierarchy": models.PayloadSchemaType.KEYWORD,
    "article_no": models.PayloadSchemaType.INTEGER,
}


def client(*, timeout_seconds: float | None = None) -> QdrantClient:
    request_bounded = timeout_seconds is not None
    return QdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,  # Qdrant Cloud 인증 (로컬은 비움)
        # Batch/ingestion keeps its existing WAN allowance.  Production
        # request paths always pass the shorter absolute-deadline-derived cap.
        timeout=timeout_seconds if timeout_seconds is not None else 60,
        # Compatibility probing is another network request.  Generation
        # runtime verifies the exact collection itself, so bounded request
        # clients must not add an implicit unbudgeted call here.
        check_compatibility=not request_bounded,
    )


@contextmanager
def bounded_client(
    deadline: RequestDeadline,
    *,
    client_factory: Callable[..., Any] = client,
) -> Iterator[Any]:
    """Yield one request client with an absolute wall-clock close watchdog.

    HTTPX's scalar timeout is a per-phase inactivity timeout, not a total
    response duration.  A peer can otherwise send one byte before each read
    timeout forever.  Closing the request-scoped transport at the absolute
    operation cutoff interrupts that slow-drip path; the caller still checks
    the shared request deadline after the operation.
    """

    instance = client_factory(
        timeout_seconds=deadline.store_operation_whole_seconds(),
    )
    state_lock = Lock()
    closed = False
    operation_expired = Event()

    def close_once() -> None:
        nonlocal closed
        with state_lock:
            if closed:
                return
            closed = True
        close = getattr(instance, "close", None)
        if callable(close):
            try:
                close()
            # The request observes transport failure; this local fail-safe
            # must not leak a watchdog exception from the timer thread.
            except Exception:  # noqa: S110  # nosec B110
                pass

    def expire_operation() -> None:
        operation_expired.set()
        close_once()

    watchdog = Timer(deadline.store_operation_seconds(), expire_operation)
    watchdog.daemon = True
    watchdog.name = "customs-evidence-qdrant-deadline"
    watchdog.start()
    try:
        yield instance
        if operation_expired.is_set():
            raise DeadlineExceeded("Qdrant operation exceeded its hard deadline")
        deadline.assert_work_allowed()
    finally:
        watchdog.cancel()
        watchdog.join(timeout=deadline.completion_reserve_seconds)
        close_once()


def ensure_collection(qc: QdrantClient | None = None) -> None:
    """컬렉션이 없으면 생성. 현행 인덱스이므로 drop-recreate 하지 않는다."""
    qc = qc or client()
    if not qc.collection_exists(COLLECTION):
        qc.create_collection(
            collection_name=COLLECTION,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(size=1024, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={
                BM25_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
    for field, schema in _PAYLOAD_INDEXES.items():
        qc.create_payload_index(COLLECTION, field_name=field, field_schema=schema)
