# Law repository

The Data Platform keeps Korean customs-law, HS, and CCCT knowledge in three
local stores:

| Store | Responsibility |
| --- | --- |
| PostgreSQL | versioned law and requirement ledger |
| Qdrant | dense/BM25 retrieval projection |
| Neo4j | article and requirement graph projection |

`docker-compose.yml` is the only Docker definition in this repository. It
binds all ports to `127.0.0.1` and is intended for a developer's workstation
or an approved local UAT operator, not an internet-facing service.

## Start a local workspace

From the repository root:

```powershell
uv sync --locked --extra rag --extra ingestion --group dev
Copy-Item law_repository/.env.example law_repository/.env
docker compose -f law_repository/docker-compose.yml up -d --wait
uv run --locked --extra rag --extra ingestion python tools/local_preflight.py --phase services
Push-Location law_repository
uv run --project .. --locked --extra rag --extra ingestion python scripts/migrate.py
Pop-Location
```

The committed HSK and CCCT source files can be ingested without downloading
external data. `src.ingest.laws` and `src.sync.delta` require approved external
credentials supplied only through the local environment.

```powershell
Push-Location law_repository
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.hsk
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.ccct --load-only
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.graph
Pop-Location
```

## Local demo and Evidence API

`python -m src.webapp` is an unauthenticated local demo. Do not expose it to
the public internet.

The authenticated Evidence runtime is created through
`src.runtime_service` with `src.runtime_settings`. Its public API remains the
versioned `/internal/rag/v1`, `/internal/rag/v2`, and `/internal/rag/v3`
contracts. Agent consumers must not connect directly to these stores.

The repository intentionally has no cloud, HF Space, image, release, or
deployment instructions. Use the shared engineering harness for everyday
checks and the separately provided operator bundle for authorized local UAT.
