"""Immutable model identities used by every production index and query path.

Model repository names are not release identities: their default branches can
move without a Data Platform source change.  A knowledge generation therefore
records and loads the exact upstream commit used for embedding and reranking.
"""

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


def release_model_inventory() -> dict[str, dict[str, str]]:
    """Return a JSON-safe immutable inventory for release manifests."""

    return {
        "dense": DENSE_MODEL.as_dict(),
        "reranker": RERANK_MODEL.as_dict(),
    }


__all__ = [
    "DENSE_MODEL",
    "RERANK_MODEL",
    "ModelIdentity",
    "release_model_inventory",
]
