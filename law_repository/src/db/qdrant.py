"""Local Qdrant collection definition for the Data profiles."""

from __future__ import annotations

from qdrant_client import QdrantClient, models

from src.config import settings

COLLECTION = "customs_law_current"
DENSE_VECTOR = "dense"
BM25_VECTOR = "bm25"

_PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "article_pk": models.PayloadSchemaType.INTEGER,
    "law_id": models.PayloadSchemaType.KEYWORD,
    "law_name": models.PayloadSchemaType.KEYWORD,
    "hierarchy": models.PayloadSchemaType.KEYWORD,
    "article_no": models.PayloadSchemaType.INTEGER,
}


def client() -> QdrantClient:
    return QdrantClient(url=settings.qdrant_url, timeout=60, check_compatibility=True)


def ensure_collection(qc: QdrantClient | None = None) -> None:
    """Create the rebuildable local collection when it does not yet exist."""

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
