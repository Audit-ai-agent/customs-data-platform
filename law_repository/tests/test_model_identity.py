from __future__ import annotations

import re

from src.embed import dense, rerank
from src.embed.model_identity import DENSE_MODEL, RERANK_MODEL, release_model_inventory


def test_production_models_are_pinned_to_full_upstream_commits() -> None:
    full_commit = re.compile(r"^[0-9a-f]{40}$")

    assert full_commit.fullmatch(DENSE_MODEL.revision)
    assert full_commit.fullmatch(RERANK_MODEL.revision)
    assert dense.DENSE_MODEL is DENSE_MODEL
    assert rerank.MODEL == RERANK_MODEL.model_id
    assert rerank.MODEL_REVISION == RERANK_MODEL.revision


def test_release_model_inventory_is_stable_and_json_safe() -> None:
    assert release_model_inventory() == {
        "dense": {
            "model_id": "BAAI/bge-m3",
            "purpose": "dense-embedding",
            "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        },
        "reranker": {
            "model_id": "BAAI/bge-reranker-v2-m3",
            "purpose": "cross-encoder-rerank",
            "revision": "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
        },
    }
