# Immutable Knowledge Generations

## Purpose and authority

This subsystem builds PostgreSQL, Qdrant, and Neo4j as one immutable,
generation-scoped release unit. It does not overwrite or delete the team's
legacy law, CCCT, HSK, vector, or graph records. PostgreSQL owns the canonical
active-generation pointer; Qdrant and Neo4j must converge to that authority.

The state machine is:

```text
building ──validate──> validated ──seal──> sealed ──promote──> active
    │                                                       │
    └──abort──> aborted                         old active ──┴──> retired
```

Only `building` may be resumed or aborted. Validated, sealed, active, retired,
and aborted generations and their child rows are immutable. Generation events
are append-only.

## Source and release identity

`generation_id` is derived from the canonical source-manifest hash,
model-manifest hash, and strict index-configuration hash. A production source
manifest is accepted only when it binds all of the following:

- the exact official 58-law target ID/name set;
- the verified collector commit and every bounded raw source byte;
- a canonical source-root SHA-256;
- the immutable article snapshot copied into the generation;
- the exact pinned CCCT and HSK source files and snapshot IDs;
- a separately hashed approval artifact and a non-expired validity window.

The official-law verifier parses the raw source set again and compares its
indexable leaves, MST values, law IDs, names, article IDs, versions, text, and
content hashes with the PostgreSQL generation snapshot. Missing files,
renamed laws, arbitrary snapshot IDs, self-asserted release flags, or expired
approvals fail before store preparation.

Dataset membership is relational and exact, not an aggregate assertion:

| Dataset member kind | Required rows |
|---|---:|
| `ccct_query` | 22,654 |
| `ccct_requirement` | 14,726 |
| `ccct_source_record` | 14,726 |
| `ccct_document` | 63 |
| `hsk_leaf` | 11,327 |

CCCT membership hashes are recomputed from the linked PostgreSQL snapshot.
HSK leaf membership and graph properties are regenerated from both pinned
XLSX sources. A validated or later generation freezes the linked CCCT
snapshot and its query, requirement, source-record, and document rows.

## Strict index configuration

The versioned `IndexConfig` schema is closed to unknown fields. Version 1.0
requires:

- one named dense vector, `dense`;
- a positive, explicitly pinned dense dimension and cosine distance;
- one named sparse vector, `bm25`, with the IDF modifier;
- generation-isolated resources and generation-tagged payloads.

Qdrant attestation reads the actual collection configuration and every
generation point with payload and vectors. It validates payload identity,
source/model hashes, text hashes, dense dimensions and finite values, sparse
indices/values, and then hashes the complete canonical projection.

Neo4j attestation reads every generation article, relation type/endpoints and
basis, and every one of the 11,327 `GenerationHSK` leaves. Each HSK node and
its `CONTAINS_HSK` relationship are bound to the generation and snapshot. The
full canonical node/edge projection is hashed.

PostgreSQL attestation hashes the complete immutable article snapshot, exact
dataset membership, and strict index configuration. A seal binds the three
stable projection hashes; observation timestamps are deliberately excluded
from that stable identity.

## Evaluation bundle and seal

Sealing does not accept a caller-created aggregate report. The coordinator
accepts only a `VerifiedEvaluationBundle` loaded from actual bounded files:

```text
report.json
runner.artifact
gold.artifact
queries.artifact
code.artifact
cases.json
approval.json
```

The loader rejects duplicate JSON keys, non-finite values, unexpected files,
hash mismatch, generation mismatch, or approval mismatch. `report.json`
contains immutable per-case structured and hybrid results. Aggregates are
recomputed from those case records, the exact case-ID set is bound, and all 58
laws must appear in the coverage map. The human approval binds the evaluation
subject, Gold artifact, case manifest, reviewer, and review time.

The gate still requires mock count and unexpected skips to be zero, structured
contract/accuracy/recall/citation completeness to be 1.0, false positives to
be zero, Hybrid Hit@5 and Recall@10 to be 1.0, and nDCG@10 to be at least 0.8.

## Build, resume, status, and abort

The CLI requires the verified official-law source root, official-law approval,
and dataset approval. Release eligibility is derived from approval validity;
there is no command-line switch that can self-assert it.

A repeated begin request is idempotent only when the existing generation is
still `building` and its complete specification and memberships are identical.
Any drift fails closed. Operators may inspect status or abort a failed
`building` generation. Active or sealed data is never deleted as cleanup.

## Promotion, rollback, and crash recovery

Promotion and rollback run under the PostgreSQL generation writer lock:

1. read the authoritative `(generation_id, epoch)` token;
2. re-attest PostgreSQL, Qdrant, and Neo4j immediately before switching;
3. compare each live projection with its sealed projection;
4. verify current external pointers;
5. switch Qdrant and Neo4j;
6. commit the PostgreSQL pointer last with the expected epoch.

If any external switch or the PostgreSQL commit is uncertain, the coordinator
re-reads PostgreSQL while holding the writer lock and converges Qdrant and
Neo4j to PostgreSQL. It never assumes an attempted compensation succeeded.
Readers compare both generation ID and epoch before and after a read, which
prevents an A→B→A pointer change from passing an ABA check.

## Migration prerequisites and operational boundary

Migration `004_knowledge_generations.sql` is additive and requires completed
001–003 migrations and the legacy parent-article key. Database triggers enforce
state-transition preconditions, immutable child rows using both OLD and NEW
generation state, append-only events, and frozen CCCT source snapshots.

Offline tests do not migrate a live database or contact Qdrant/Neo4j. Before a
production release, operators must still:

1. supply and approve a real, current official 58-law corpus;
2. supply current CCCT/HSK approval artifacts;
3. build a real per-case RAG evaluation bundle and obtain human approval;
4. apply migration 004 to an ephemeral stack;
5. build, attest, promote, read, rollback, and reconcile against live
   PostgreSQL/Qdrant/Neo4j;
6. archive the exact run manifest and store projections.

Until those live and human-reviewed gates pass, this subsystem is
implementation-complete but not a production data release.
