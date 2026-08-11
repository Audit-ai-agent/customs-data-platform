# Local knowledge stores and Evidence API

The Data Platform uses three local stores:

| Store | Responsibility |
| --- | --- |
| PostgreSQL | versioned law and requirement ledger |
| Qdrant | dense/BM25 retrieval projection |
| Neo4j | article and HSK/requirement graph |

`docker-compose.yml` is a local-only store definition. It exposes ports only
on `127.0.0.1`, uses immutable image digests, and relies on a profile-specific
Compose project name rather than fixed container names.

Do not copy this repository's `.env.example` into a shared secret file. The
profile commands generate random local database passwords outside Git:

```powershell
# Optional real-store Data development
uv run --locked --no-sync --group dev python tools/dev_store.py init
uv run --locked --no-sync --group dev python tools/dev_store.py compose up
# After a complete law/HSK/CCCT build only: full current-law/index/graph comparison.
uv run --locked --no-sync --group dev --extra store python tools/dev_store.py verify

# Default real 65-law Evidence-history UAT
uv sync --locked --group dev --extra store --extra rag --extra ingestion --python 3.11
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py init
# Add only your personal LAW_API_OC to this new ignored profile config.
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py compose up
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py build --as-of 2026-08-05
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py verify-store
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_law_history.py serve --host 127.0.0.1 --port 18083

# Legacy three-law compatibility UAT
uv sync --locked --group dev --extra store --extra rag --extra ingestion --python 3.11
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py init
# Add only your personal LAW_API_OC to the ignored _local profile config.
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py compose up
# Build the bounded three-law stores before requesting a full comparison.
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py build --as-of 2026-08-05
# Optional after build or a Data/schema/retrieval change; not a normal UAT step.
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py verify-store
```

If a completed three-law run must be rebuilt, use the explicit scoped reset:

```powershell
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py reset `
  --confirm-profile functional-uat-3law
```

It removes only the matching Compose project's volumes and active manifest;
raw local source files and `_artifacts` outputs remain intact.

For normal team full-stack development, do not start stores. Use
`tools/run_dev_fixture_evidence.py` from the repository root. It provides the
authenticated Evidence V3 endpoints on loopback with deterministic synthetic
data and a generated local token. Its base setup requires only FastAPI,
Pydantic, Uvicorn, and the development test tools; it does not install store
clients or model packages.

The default law-history builder uses the official Law API only for the fixed
65-law candidate registry and its explicit refresh window. It requires a
personal `LAW_API_OC` placed in its own ignored profile config, stores raw
official responses below that profile and models at `_local/cache/huggingface`,
and records only safe profile/date/count/model summaries outside local state.
Every version receipt retains the official URI, hash, collection time,
effective date, and article count locally. HSK and CCCT remain the fixed
`2026-08-05` snapshot and their mismatch with a later legal date is returned
as `partial` with explicit `temporal_context`; no current law text is
substituted for an unavailable historical date.

The HSK/CCCT date is a contract baseline, not a claimed external collection
timestamp. The profile verifies the exact tracked input bytes and their local
PostgreSQL/Neo4j projections; it does not fabricate a separate agency receipt
that is absent from those source files.

### Dated HSK/CCCT archives

An operator may add a genuinely dated official HSK/CCCT archive under the
ignored `_local/data/official-archives/hsk-ccct/YYYY-MM-DD/` location. Its
canonical `archive.manifest.json` binds two official HSK XLSX files and one
complete page-by-page CCCT query-receipt file to official URI, byte size,
SHA-256, published/effective dates, query count, empty-query count, and
raw-row count. The importer has no downloader, service key, clone fallback,
or current-ledger backfill:

The exact safe input shape is checked in as
`law_repository/schemas/hsk_ccct_official_archive.schema.json`; its runtime
validator additionally requires canonical JSON bytes, local non-symlink
paths, receipt hashes, page completeness, and provenance dates at or before
the archive date. The schema is metadata only—no archive body or API key is
tracked.

```powershell
uv run --locked --no-sync --group dev --extra store --extra ingestion python tools/hsk_ccct_archive.py validate --as-of 2026-01-01
uv run --locked --no-sync --group dev --extra store --extra ingestion python tools/hsk_ccct_archive.py import --as-of 2026-01-01
# An explicit second action is required before a service can use that date.
uv run --locked --no-sync --group dev --extra store --extra ingestion python tools/hsk_ccct_archive.py activate --as-of 2026-01-01
```

Import writes immutable PostgreSQL HSK node versions and binds them to an
archive CCCT snapshot; it never overwrites the current Neo4j HSK/Requirement
projection. The service captures the active archive-pointer hash at startup
and returns restart-required 503 if it changes. A date without an activated
official archive is returned as `partial` with additive `temporal_coverage`
(`law`, `hsk`, `requirements`); HSK/CCCT evidence is not substituted from the
2026-08-05 baseline. The baseline itself remains the retained fixed-input
compatibility date.

The full source inventory remains in ignored local state.
`verify-store` is the explicit full ID/hash comparison for Data maintenance;
the regular UAT server uses lightweight source/cache/count checks. The CCCT
reference input is already tracked, so no second API credential or bulk
collection command is required. It does not represent a Production deployment
or approval. The `serve` command deliberately removes the collection key from
its process environment and settings before accepting any Evidence request.

## Korean trade guidance source-build boundary

The broader `functional-uat-kr-trade` profile is deliberately separate from
both Evidence profiles. It may be initialized and inspected without changing
the law-history or legacy three-law source files, stores, model cache, or
manifests:

```powershell
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py init
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py status
```

It does not fetch external guidance. A registry declaration for
국가법령정보센터, 관세청, FTA 포털, 관계기관, or KOTRA only permits an operator to
stage already-obtained, official source bytes below the ignored local profile.
The explicit build command accepts only those allowlisted source IDs, the
exact corresponding registry `official_uri` as an operator-attested
`source_uri`, profile-relative paths, matching SHA-256 values, source
collection time, registry topic/direction, and short excerpts that actually
occur in the hash-verified UTF-8 source text. It never infers a collection URL
from raw HTML.

```powershell
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py build `
  --source-manifest data/guidance-sources/20260805-example/source-build.json
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py verify
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py serve-guidance
```

`build` creates an immutable local manifest with `as_of`, source ID/URI/hash,
collection time, topic/direction coverage, and derived record IDs. `verify`
rechecks every source hash before readiness or a query. `as_of` is a knowledge
snapshot reference date, not a finding about an agency guide's legal effective
date; consumers must not present it as legal-effectivity proof. `source_build_required`
and the legacy receipt-only `registered_not_indexed` state remain fail-closed.
Only `ready` can serve the guidance-only
`POST /internal/rag/v3/guidance/query` endpoint.

Each query response includes the provider registry's `registry_sha256`; each
returned derived record includes its stable `registry_source_id`. They bind the
displayed official URI to the safe checked-in registry manifest without adding
raw source text or local receipt paths to the HTTP contract.

This endpoint is not a Data-store build: it deliberately does not expose
document-review `/retrieve`, PostgreSQL/Qdrant/Neo4j retrieval, or a claim of
complete Korean trade-law coverage. Those require a separate verified
three-store build; this source-build path does not reset, start, or reuse one.

### V2 page-level registry and runtime

V2 is a separate additive snapshot. It preserves the V1/r3 registry and all
three-law state, and declares 11 topics, 18 closed scopes, and 28 applicable
direction bundles in `src/guidance_registry_v2.json`. The safe consumer copy
is `../contracts/evidence/v3/guidance_registry_v2.manifest.json`; it contains
official URLs and scope metadata only, never raw source text, local paths, or
source hashes.

```powershell
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py build-v2 `
  --source-manifest data/guidance-sources/<staging>/source-build-v2.json
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py verify-v2
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py serve-guidance-v2 `
  --host 127.0.0.1 --port 18094
```

V2 never fetches from those URLs. The explicit build verifies each exact URL,
byte hash, collection time, scope/direction binding, and quoted excerpt. A
bundle is `covered` only after all its required page records are present. Law
current-status pages must prove their effective date at `as_of`; linked body
pages inherit it only from that verified source. PDF citations require both
the raw-PDF hash and a hash-bound local text projection. All of those receipts
remain below ignored `_local` state.

The V2 server is loopback-only at
`http://127.0.0.1:18094/internal/rag/v3/guidance/query`; it uses the profile's
local bearer token and `X-Customs-Service-Mode: functional-uat`. The
authenticated readiness endpoint is `/internal/rag/v3/health/ready`; it and
the query fail closed when V2 state cannot revalidate. Consumers must compare
the V2 query response's `registry_version`, `registry_sha256`, `snapshot_id`,
and `as_of` before displaying any official URI.
