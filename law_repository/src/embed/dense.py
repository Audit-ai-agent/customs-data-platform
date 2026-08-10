"""bge-m3 dense 임베딩 (설계 §4.2 — 'Vector 검색 근거').

모델 로드가 무거우므로 lazy 싱글턴. 인덱싱·검색(Phase 5) 양쪽이 이걸 공유해야
같은 공간에서 비교된다.
"""

from __future__ import annotations

import numpy as np
from threading import Lock
from typing import Any, Literal

from src.embed.model_identity import DENSE_MODEL

DenseDevice = Literal["auto", "cpu", "cuda"]

_models: dict[DenseDevice, Any] = {}
_model_lock = Lock()
_encode_lock = Lock()


class DenseDeviceUnavailable(RuntimeError):
    """The requested pinned dense-inference device is unavailable."""


def _resolve_device(requested: DenseDevice | None) -> DenseDevice:
    """Keep law-history inference CPU-bound unless a local scope permits CUDA."""

    from src.local_profiles import (
        law_history_cuda_opt_in_enabled,
        law_history_profile_is_active,
    )

    if not law_history_profile_is_active():
        return requested or "auto"
    if requested in (None, "auto"):
        return "cuda" if law_history_cuda_opt_in_enabled() else "cpu"
    if requested == "cuda" and not law_history_cuda_opt_in_enabled():
        raise DenseDeviceUnavailable("law-history CUDA requires explicit local opt-in")
    assert requested is not None
    return requested


def _get_model(device: DenseDevice):
    model = _models.get(device)
    if model is None:
        with _model_lock:
            model = _models.get(device)
            if model is None:
                import torch
                from FlagEmbedding import BGEM3FlagModel
                from huggingface_hub import snapshot_download

                if device == "cuda" and not torch.cuda.is_available():
                    raise DenseDeviceUnavailable("CUDA dense inference is unavailable")
                # B615 is suppressed only at this exact call: model_identity.py
                # pins a full 40-character revision and runtime is cache-only.
                model_path = snapshot_download(  # nosec B615
                    repo_id=DENSE_MODEL.model_id,
                    revision=DENSE_MODEL.revision,
                    # Local setup must prefetch this immutable revision. Runtime
                    # lookup must never contact or trust a mutable remote model
                    # repository.
                    local_files_only=True,
                )
                # ``devices='cpu'`` is material: merely disabling fp16 lets
                # FlagEmbedding select a visible CUDA device by itself. The
                # retained profiles preserve their historical ``auto`` path.
                kwargs: dict[str, object] = {
                    "use_fp16": (
                        torch.cuda.is_available() if device == "auto" else device == "cuda"
                    )
                }
                if device != "auto":
                    kwargs["devices"] = device
                model = BGEM3FlagModel(model_path, **kwargs)
                _models[device] = model
    assert model is not None
    return model


def encode(
    texts: list[str],
    batch_size: int = 8,
    *,
    device: DenseDevice | None = None,
) -> np.ndarray:
    """(n, 1024) float32 dense 벡터."""
    # FastAPI executes sync handlers in a worker pool.  FlagEmbedding/PyTorch
    # inference on the shared singleton is serialized to avoid concurrent
    # mutation and unbounded CPU/RAM spikes; request-level parallelism remains
    # available for PostgreSQL/Qdrant/Neo4j work.
    with _encode_lock:
        out = _get_model(_resolve_device(device)).encode(
            texts, batch_size=batch_size, max_length=8192
        )
    return np.asarray(out["dense_vecs"], dtype="float32")


__all__ = ["DenseDevice", "DenseDeviceUnavailable", "encode"]
