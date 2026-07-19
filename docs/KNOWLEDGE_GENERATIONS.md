# Immutable Knowledge Generations

## Scope

The generation subsystem prepares PostgreSQL, Qdrant, and Neo4j as one
release unit without changing the legacy tables, collections, or graph nodes.
It does not yet change `evidence.py`; that integration must resolve the
PostgreSQL active pointer and query generation-specific resources in a
separate reviewed change.

The PostgreSQL `knowledge_generation_active_pointer` is the final authority.
A generation moves through:

```text
building → validated → sealed → active → retired
                                      ↑          │
                                      └ rollback─┘
```

- `building`: sources and isolated projections may be written.
- `validated`: PostgreSQL, Qdrant, and Neo4j article attestations match.
- `sealed`: Production RAG evaluation and human-reviewed Gold pass.
- `active`: the O(1) PostgreSQL pointer exposes this generation.
- `retired`: immutable rollback target.

## Generation identity

`generation_id` is the canonical SHA-256 of:

- the 58-law + HSK + CCCT source manifest hash;
- the dense, sparse, and reranker model manifest hash;
- the indexing configuration hash.

The source manifest rejects missing or renamed target laws, query-bearing
source URLs, mutable model aliases, duplicate files, non-canonical ordering,
and non-finite JSON values.

The tracked CCCT baseline is explicitly bound to:

| Item | Value |
|---|---|
| source | `law_repository/data/ccct_progress.jsonl` |
| source SHA-256 | `3125d5f83290136528e520a37fbbafc45f7f4ea57dbc7824e2203708a971e5c1` |
| source revision | `4ef50b1` (2026-07-06) |
| derived snapshot | `ccct_212f4c459765d5a79f5a42e2eef77bcbe43bd7adb5c4d526a9ecc3588622e107` |
| queries | 22,654 |
| unique HSK codes | 11,327 |
| import/export variants | 2 |
| source/requirement rows | 14,726 |
| source documents | 63 |

This snapshot is not automatically treated as current forever.  Its
`freshness.status`, observation time, validity limit, limitation text, and
`release_eligible` decision are separate canonical fields.  A source marked
not release-eligible blocks sealing.

The HSK baseline binds both original files:

| File | Bytes | SHA-256 |
|---|---:|---|
| `관세청_HS부호 단위별 품목명_20260101.xlsx` | 800,868 | `9a5a942b5e44a817bf3fb9ef6145fa23ef110a9692e4f3e0b84eb33672409ba2` |
| `관세청_HS부호_20260101.xlsx` | 1,419,595 | `020661f75ac044be0eec049013b074be77ca8fb6d13f8283666f1f3e594054e5` |

The parser attestation requires 17,072 unique hierarchy nodes, 11,327 leaf
nodes, 11,327 leaf attributes, and zero missing leaf attributes.

## Cross-store attestation

Every store emits the same generation ID, model-manifest hash, article count,
article-ID-set hash, and `(article ID, content hash)` set hash.

- PostgreSQL copies the current indexable article identity, text, version, and
  hash into an immutable generation snapshot. The migration trigger rejects a
  snapshot that differs from the legacy ledger at copy time; later legacy
  in-place updates cannot rewrite the sealed generation.
- Qdrant uses `customs_law_gen_<generation_id>` and atomically switches the
  `customs_law_active` alias.
- Neo4j uses generation-tagged `GenerationArticle` nodes and one
  `KnowledgeGenerationPointer`.

The three attestations must match byte-for-byte on their common invariants.
Legacy `customs_law_current` and legacy `Article` nodes remain untouched.

## Seal gate

Sealing requires a canonical RAG report with:

- actual Production data and mock count zero;
- unexpected skips zero;
- human-reviewed Gold with all 58 laws represented;
- at least 58 positive, one negative, and one ambiguous case;
- Structured contract/accuracy/recall/metadata metrics at 1.0 and false
  positives at 0;
- Hybrid Hit@5 and Recall@10 at 1.0, nDCG@10 at least 0.8, and citation
  metadata completeness at 1.0.

The report hash, three store-attestation hashes, source hash, model hash, and
index-config hash form the immutable seal attestation.

## Promotion and rollback

`GenerationCoordinator` holds a PostgreSQL transaction advisory writer lock.
It verifies that all active pointers match the current PostgreSQL pointer,
then switches Qdrant and Neo4j, and commits PostgreSQL last.

If a switch or PostgreSQL commit fails, already-switched external stores are
restored to the previous generation.  The previous PostgreSQL active pointer
remains unchanged.  `reconcile_external_to_postgres()` is the crash-recovery
hook; PostgreSQL remains authoritative.

Evidence reads should use `guarded_active_generation()`:

1. read the O(1) PostgreSQL active generation;
2. query generation-specific Qdrant/Neo4j resources;
3. read the PostgreSQL pointer again;
4. discard the result if the generation changed.

## Migration safety

`scripts/migrate.py` now:

- owns one PostgreSQL advisory lock for the complete sequence;
- records exact SHA-256 and byte size;
- verifies every applied migration on every run;
- adopts checksum-less 001–003 only when their bytes match the embedded
  pre-hardening baseline;
- rejects removed, renumbered, modified, non-UTF-8, or concurrent migrations.

No migration or live database operation is performed by the offline test
suite.
