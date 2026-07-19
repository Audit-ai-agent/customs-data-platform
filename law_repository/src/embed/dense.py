"""bge-m3 dense 임베딩 (설계 §4.2 — 'Vector 검색 근거').

모델 로드가 무거우므로 lazy 싱글턴. 인덱싱·검색(Phase 5) 양쪽이 이걸 공유해야
같은 공간에서 비교된다.
"""

from __future__ import annotations

import numpy as np
from threading import Lock

from src.embed.model_identity import DENSE_MODEL

_model = None
_model_lock = Lock()
_encode_lock = Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                import torch
                from FlagEmbedding import BGEM3FlagModel
                from huggingface_hub import snapshot_download

                model_path = snapshot_download(
                    repo_id=DENSE_MODEL.model_id,
                    revision=DENSE_MODEL.revision,
                )
                _model = BGEM3FlagModel(
                    model_path,
                    use_fp16=torch.cuda.is_available(),
                )
    return _model


def encode(texts: list[str], batch_size: int = 8) -> np.ndarray:
    """(n, 1024) float32 dense 벡터."""
    # FastAPI executes sync handlers in a worker pool.  FlagEmbedding/PyTorch
    # inference on the shared singleton is serialized to avoid concurrent
    # mutation and unbounded CPU/RAM spikes; request-level parallelism remains
    # available for PostgreSQL/Qdrant/Neo4j work.
    with _encode_lock:
        out = _get_model().encode(texts, batch_size=batch_size, max_length=8192)
    return np.asarray(out["dense_vecs"], dtype="float32")
