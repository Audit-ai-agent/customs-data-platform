# Data Platform Architecture

## Responsibility

The platform owns Korean customs knowledge ingestion, PostgreSQL/Qdrant/Neo4j
local stores, and the versioned Evidence HTTP schemas consumed by the Agent.
The Agent is the only sibling service that calls Evidence; no sibling imports
Data code or accesses these stores directly.

```text
official law / tracked HSK + CCCT inputs
  -> PostgreSQL ledger
  -> Qdrant and Neo4j projections
  -> Evidence V3 HTTP
  -> AI Agent
```

PostgreSQL remains the source of truth. Qdrant and Neo4j are local,
rebuildable projections. Missing data, ambiguous HS resolution, or store
failure returns `partial` or `failed`; it never becomes a claim that no
requirement exists.

## Profiles

| Profile | Purpose | Dependencies | Result boundary |
| --- | --- | --- | --- |
| `dev-fixture` | normal full-stack development | Python only | deterministic synthetic Evidence, human review only |
| `dev-store` | Data implementation work | local Docker | isolated disposable stores |
| `functional-uat-law-history` | default real Evidence UAT | local Docker, Law API, model cache | 65-law temporal Evidence; legal date plus explicit fixed HSK/requirements dates |
| `functional-uat-3law` | legacy compatibility check | local Docker, Law API, model cache | bounded three-law real Data state, human review only |

All profile services bind to literal loopback addresses. The caller must send a
profile-specific bearer token and `X-Customs-Service-Mode`; Data returns the
same mode header. This prevents a development fixture and actual functional
UAT from being combined accidentally.

## Contracts and UAT boundary

`contracts/evidence/v3/` remains the canonical Evidence request/response
shape. The local profile servers preserve that JSON body. Readiness is limited
to an operational `status`; the mode response header is the local-profile
binding. It carries no release, signature, trust, approval, quality-gate, or
deployment-admission metadata.

The law-history active manifest retains local-only official source receipts
(law ID, MST, URI, hash, collection time, effective date, and article count)
and the all-version temporal Qdrant projection. The public V3 response adds
only `temporal_context` dates. Qdrant filters the half-open legal validity
interval before dense/BM25 ranking and PostgreSQL confirms the exact row; it
does not retrieve a current article and substitute an old body afterwards.
The retained HSK/CCCT `2026-08-05` values are a fixed input-contract baseline:
their exact tracked bytes and projections are attested, but the source files
do not claim a separately verifiable external collection timestamp.
The three-law manifest remains compatible. Compose pins local store images;
runtime does not inspect Docker image metadata. Models are cached once at
`_local/cache/huggingface`. The bounded CCCT reference input is tracked and
loaded locally; no data.go.kr credential or bulk refresh path remains.
Passwords, bearer tokens, Docker volumes, and output artifacts stay outside
Git.

`/health/ready` checks profile state, store connectivity, and bounded counts.
Functional `/retrieve` rechecks its profile-owned state before dispatch so a
post-start source mutation cannot bypass the local guard. The full
PostgreSQL/Qdrant/Neo4j ID and law-hash comparison is the explicit
`functional_uat_law_history.py verify-store` maintenance command, not a
Production or deployment gate.
