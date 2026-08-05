"""Pinned model identities used by local store indexing and retrieval."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    model_id: str
    revision: str
    purpose: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


DENSE_MODEL = ModelIdentity(
    model_id="BAAI/bge-m3",
    revision="5617a9f61b028005a4858fdac845db406aefb181",
    purpose="dense-embedding",
)

RERANK_MODEL = ModelIdentity(
    model_id="BAAI/bge-reranker-v2-m3",
    revision="953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    purpose="cross-encoder-rerank",
)


def model_inventory() -> dict[str, dict[str, str]]:
    """Return the JSON-safe model inventory recorded in local UAT manifests."""

    return {
        "dense": DENSE_MODEL.as_dict(),
        "reranker": RERANK_MODEL.as_dict(),
    }


__all__ = [
    "DENSE_MODEL",
    "RERANK_MODEL",
    "ModelIdentity",
    "model_inventory",
]
