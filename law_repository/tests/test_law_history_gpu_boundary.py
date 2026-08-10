"""Law-history CUDA must be an explicit local operator choice."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pytest

from src.local_profiles import (
    FUNCTIONAL_UAT_LAW_HISTORY_PROFILE,
    functional_uat_law_history_cuda_opt_in,
    law_history_cuda_opt_in_enabled,
)


def test_cuda_scope_is_process_local_nest_safe_and_ignores_ambient_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUSTOMS_DATA_PROFILE", FUNCTIONAL_UAT_LAW_HISTORY_PROFILE)
    monkeypatch.setenv("CUSTOMS_LAW_HISTORY_CUDA_OPT_IN", "1")

    assert law_history_cuda_opt_in_enabled() is False
    with functional_uat_law_history_cuda_opt_in():
        assert law_history_cuda_opt_in_enabled() is True
        with functional_uat_law_history_cuda_opt_in():
            assert law_history_cuda_opt_in_enabled() is True
        assert law_history_cuda_opt_in_enabled() is True
    assert law_history_cuda_opt_in_enabled() is False


def test_cuda_scope_cannot_enable_another_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOMS_DATA_PROFILE", "functional-uat-3law")

    with functional_uat_law_history_cuda_opt_in():
        assert law_history_cuda_opt_in_enabled() is False


@pytest.mark.skipif(importlib.util.find_spec("numpy") is None, reason="rag extra is not installed")
def test_dense_law_history_defaults_to_cpu_even_if_cuda_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.embed import dense

    calls: list[dict[str, object]] = []

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

    class FakeDenseModel:
        def __init__(self, _model_path: str, **kwargs: object) -> None:
            calls.append(dict(kwargs))

        def encode(self, texts: list[str], **_kwargs: object) -> dict[str, list[list[float]]]:
            return {"dense_vecs": [[float(index)] for index, _text in enumerate(texts)]}

    torch = ModuleType("torch")
    torch.cuda = FakeCuda()  # type: ignore[attr-defined]
    flag_embedding = ModuleType("FlagEmbedding")
    flag_embedding.BGEM3FlagModel = FakeDenseModel  # type: ignore[attr-defined]
    hub = ModuleType("huggingface_hub")
    hub.snapshot_download = lambda **_kwargs: "/pinned/model"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", flag_embedding)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(dense, "_models", {})
    monkeypatch.setenv("CUSTOMS_DATA_PROFILE", FUNCTIONAL_UAT_LAW_HISTORY_PROFILE)

    dense.encode(["법령"])

    assert calls == [{"use_fp16": False, "devices": "cpu"}]


@pytest.mark.skipif(importlib.util.find_spec("numpy") is None, reason="rag extra is not installed")
def test_dense_law_history_allows_cuda_only_inside_local_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.embed import dense

    calls: list[dict[str, object]] = []

    class FakeCuda:
        @staticmethod
        def is_available() -> bool:
            return True

    class FakeDenseModel:
        def __init__(self, _model_path: str, **kwargs: object) -> None:
            calls.append(dict(kwargs))

        def encode(self, texts: list[str], **_kwargs: object) -> dict[str, list[list[float]]]:
            return {"dense_vecs": [[float(index)] for index, _text in enumerate(texts)]}

    torch = ModuleType("torch")
    torch.cuda = FakeCuda()  # type: ignore[attr-defined]
    flag_embedding = ModuleType("FlagEmbedding")
    flag_embedding.BGEM3FlagModel = FakeDenseModel  # type: ignore[attr-defined]
    hub = ModuleType("huggingface_hub")
    hub.snapshot_download = lambda **_kwargs: "/pinned/model"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "FlagEmbedding", flag_embedding)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(dense, "_models", {})
    monkeypatch.setenv("CUSTOMS_DATA_PROFILE", FUNCTIONAL_UAT_LAW_HISTORY_PROFILE)

    with pytest.raises(dense.DenseDeviceUnavailable, match="explicit local opt-in"):
        dense.encode(["법령"], device="cuda")
    with functional_uat_law_history_cuda_opt_in():
        dense.encode(["법령"])

    assert calls == [{"use_fp16": True, "devices": "cuda"}]


def test_reranker_rejects_cuda_without_law_history_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.embed import rerank

    monkeypatch.setenv("CUSTOMS_DATA_PROFILE", FUNCTIONAL_UAT_LAW_HISTORY_PROFILE)

    assert rerank._cuda_allowed_for_active_profile() is False
    with functional_uat_law_history_cuda_opt_in():
        assert rerank._cuda_allowed_for_active_profile() is True


@pytest.mark.skipif(importlib.util.find_spec("numpy") is None, reason="rag extra is not installed")
def test_legacy_dense_auto_device_behavior_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.embed import dense

    monkeypatch.setenv("CUSTOMS_DATA_PROFILE", "functional-uat-3law")

    assert dense._resolve_device(None) == "auto"
    assert dense._resolve_device("cuda") == "cuda"
