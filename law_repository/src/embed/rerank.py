"""bge-reranker-v2-m3 cross-encoder (A1 rerank).

FlagEmbedding 의 FlagReranker 는 transformers 5.x 에서 깨진다
(compute_score 가 제거된 tokenizer.prepare_for_model 을 호출 —
2026-07-12 실측 AttributeError). bge-m3 임베딩 쪽은 이 경로를 안 타서
무사하므로, reranker 만 transformers 를 직접 써서 구현한다.

모델이 무겁고(≈2.3GB, 최초 1회 HF 다운로드) CPU 추론이라 lazy 싱글턴.
로드/추론 실패 시 None 을 반환해 호출부(A1)가 RRF 순위로 degrade 하게
한다 — 검색 자체는 죽지 않는다.
"""

from __future__ import annotations

import math

from src.embed.model_identity import RERANK_MODEL

MODEL = RERANK_MODEL.model_id
MODEL_REVISION = RERANK_MODEL.revision
_BATCH = 4  # CPU 메모리 상한 (568M 파라미터 × max_length 토큰)

_tok = None
_model = None
_failed = False


class RerankUnavailable(RuntimeError):
    """The pinned reranker cannot produce a complete, bounded score batch."""


def _get():
    global _tok, _model, _failed
    if _model is None and not _failed:
        try:
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            # B615 is suppressed only on these calls: MODEL_REVISION is a full
            # immutable SHA from model_identity.py and lookup is cache-only.
            _tok = AutoTokenizer.from_pretrained(  # nosec B615
                MODEL,
                revision=MODEL_REVISION,
                local_files_only=True,
            )
            _model = AutoModelForSequenceClassification.from_pretrained(  # nosec B615
                MODEL,
                revision=MODEL_REVISION,
                local_files_only=True,
            )
            _model.eval()
        except Exception as e:  # noqa: BLE001 — 어떤 실패든 degrade 가 계약
            print(f"[rerank] {MODEL} 로드 실패 — RRF 순위로 degrade: {e}")
            _failed = True
    return _model


def available() -> bool:
    return _get() is not None


def scores(query: str, passages: list[str], max_length: int = 1024) -> list[float] | None:
    """쿼리-지문 쌍별 [0,1] 정규화(sigmoid) 점수. 모델 불가 시 None."""
    global _failed
    if _get() is None:
        return None
    if not passages:
        return []
    try:
        import torch

        out: list[float] = []
        with torch.no_grad():
            for i in range(0, len(passages), _BATCH):
                chunk = passages[i : i + _BATCH]
                batch = _tok(
                    [query] * len(chunk),
                    chunk,
                    padding=True,
                    truncation=True,
                    max_length=max_length,
                    return_tensors="pt",
                )
                logits = _model(**batch).logits.squeeze(-1)
                out.extend(torch.sigmoid(logits).tolist())
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[rerank] 추론 실패 — RRF 순위로 degrade: {e}")
        _failed = True
        return None


def required_scores(query: str, passages: list[str], max_length: int = 1024) -> list[float]:
    """Return validated scores or fail closed.

    The general-purpose hybrid explorer may still use :func:`scores` and its
    documented RRF fallback.  Runtime profiles that attest BGE reranking as a
    required retrieval stage use this strict boundary instead, so a missing
    model or malformed inference result cannot be presented as a successful
    reranked search.
    """

    result = scores(query, passages, max_length=max_length)
    if result is None:
        raise RerankUnavailable("pinned reranker is unavailable")
    if len(result) != len(passages):
        raise RerankUnavailable("pinned reranker returned an incomplete score batch")
    normalized = [float(value) for value in result]
    if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in normalized):
        raise RerankUnavailable("pinned reranker returned an invalid score")
    return normalized


__all__ = [
    "MODEL",
    "MODEL_REVISION",
    "RerankUnavailable",
    "available",
    "required_scores",
    "scores",
]
