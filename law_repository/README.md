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
