"""Local Qdrant collection definition for the Data profiles."""

from __future__ import annotations

from qdrant_client import QdrantClient, models

from src.config import settings

COLLECTION = "customs_law_current"
TEMPORAL_COLLECTION = "customs_law_temporal"
DENSE_VECTOR = "dense"
BM25_VECTOR = "bm25"

_PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    "article_pk": models.PayloadSchemaType.INTEGER,
    "law_id": models.PayloadSchemaType.KEYWORD,
    "law_name": models.PayloadSchemaType.KEYWORD,
    "hierarchy": models.PayloadSchemaType.KEYWORD,
    "article_no": models.PayloadSchemaType.INTEGER,
}

_TEMPORAL_PAYLOAD_INDEXES: dict[str, models.PayloadSchemaType] = {
    **_PAYLOAD_INDEXES,
    # Qdrant range filters use an integer day value.  The matching PostgreSQL
    # query still applies the canonical half-open date interval, so this is a
    # retrieval-time candidate filter rather than the authority for validity.
    "valid_from_ordinal": models.PayloadSchemaType.INTEGER,
    "valid_to_ordinal": models.PayloadSchemaType.INTEGER,
    # These are not ranking fields.  They make the full profile verifier
    # compare each temporal vector to its immutable PostgreSQL article/version
    # receipt instead of accepting a matching aggregate count.
    "version_mst": models.PayloadSchemaType.KEYWORD,
    "version_key": models.PayloadSchemaType.KEYWORD,
    "content_hash": models.PayloadSchemaType.KEYWORD,
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


def ensure_temporal_collection(qc: QdrantClient | None = None) -> None:
    """Create the all-version law collection used only by law-history.

    The retained ``customs_law_current`` collection is intentionally left
    untouched: historical retrieval must filter candidates *before* ranking
    rather than retrieve current articles and substitute an old PG version
    afterwards.
    """

    qc = qc or client()
    if not qc.collection_exists(TEMPORAL_COLLECTION):
        qc.create_collection(
            collection_name=TEMPORAL_COLLECTION,
            vectors_config={
                DENSE_VECTOR: models.VectorParams(size=1024, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={
                BM25_VECTOR: models.SparseVectorParams(modifier=models.Modifier.IDF)
            },
        )
    for field, schema in _TEMPORAL_PAYLOAD_INDEXES.items():
        qc.create_payload_index(TEMPORAL_COLLECTION, field_name=field, field_schema=schema)
