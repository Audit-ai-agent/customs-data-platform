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

# Real three-law functional UAT
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

The functional UAT builder uses the official Law API only for the bounded
three-law source collection. It requires a personal `LAW_API_OC` placed in its
ignored profile config, stores raw official responses below that profile and
models at `_local/cache/huggingface`, and records only profile, date, law IDs,
source hashes, model revisions, store counts, and build time in the shareable
UAT manifest. The full source inventory remains in ignored local state.
`verify-store` is the explicit full ID/hash comparison for Data maintenance;
the regular UAT server uses lightweight source/cache/count checks. The CCCT
reference input is already tracked, so no second API credential or bulk
collection command is required. It does not represent a Production deployment
or approval. The `serve` command deliberately removes the collection key from
its process environment and settings before accepting any Evidence request.

## Korean trade guidance source-build boundary

The broader `functional-uat-kr-trade` profile is deliberately separate from
this three-law profile. It may be initialized and inspected without changing
the existing three-law source files, stores, model cache, or manifests:

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
