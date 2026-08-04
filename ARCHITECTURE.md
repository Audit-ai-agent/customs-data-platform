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
| `functional-uat-3law` | actual local feature check | local Docker, Law API, model cache | bounded real Data state, human review only |

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

The functional UAT manifest contains only profile, date, law IDs, official
source hashes/MSTs, source-collection time, model IDs/revisions, store counts,
image digests, and a build timestamp. Raw source stays under the profile-local
`_local` path; models are cached once at `_local/cache/huggingface`. The bounded CCCT
reference input is tracked and loaded locally; no data.go.kr credential or
bulk refresh path remains. Passwords, bearer tokens, Docker volumes, and
output artifacts stay outside Git.
