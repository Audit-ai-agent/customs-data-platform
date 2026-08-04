# Customs Data Platform

Korean customs-law ingestion, three local knowledge stores, and the typed
Evidence API consumed by `customs-ai-agent`. The repository owns public
Evidence schemas and provider-context generation; it does not publish a
deployment image, cloud recipe, or release workflow.

## Team development

Clone this repository beside the other product repositories and the shared
`customs-engineering-harness`. The normal team path is:

```powershell
Set-Location ..\customs-engineering-harness
.\harness\eng.ps1 doctor
.\harness\eng.ps1 setup --repo customs-data-platform
.\harness\eng.ps1 check quick --repo customs-data-platform
.\harness\eng.ps1 check contract --repo customs-data-platform
```

The platform never imports sibling repositories or grants them direct access
to PostgreSQL, Qdrant, or Neo4j. Agent consumers use the versioned Evidence
HTTP schemas in `contracts/evidence/`.

## Local knowledge stores

For ingestion or local database work, use Docker Desktop and Python 3.11.
Credentials and downloaded source data stay in the ignored `law_repository/.env`
and `law_repository/data/raw/` paths.

```powershell
uv sync --locked --extra rag --extra ingestion --group dev
Copy-Item law_repository/.env.example law_repository/.env
docker compose -f law_repository/docker-compose.yml up -d --wait
uv run --locked --extra rag --extra ingestion python tools/local_preflight.py --phase services
Push-Location law_repository
uv run --project .. --locked --extra rag --extra ingestion python scripts/migrate.py
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.hsk
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.laws
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.ccct --load-only
uv run --project .. --locked --extra rag --extra ingestion python -m src.ingest.graph
Pop-Location
```

`src.webapp` is a local demo only. Bind it only to a trusted local network; it
is not an Agent runtime endpoint.

## Contracts and provider context

Contract byte drift is checked locally and in the repository CI:

```powershell
uv run --locked --extra rag python tools/check_contract_drift.py
```

An approved operator can make a secret-free provider context from one clean
commit. The output directory must be outside the repository. It contains only
runtime source, canonical schemas, `source-manifest.json`, and provider digest
documents; it never copies data, secrets, tests, Docker assets, or Git metadata.

```powershell
uv run --locked --extra rag python tools/build_provider_context.py `
  --repository . --output <EXTERNAL_PROVIDER_CONTEXT>
```

The generic v2/v3 conformance servers accept `--provider-context` and a
separately supplied token file. They are test fixtures, not a deployment path.
Authorized local UAT is delivered as a Git-external operator bundle; its data,
secrets, state, and results remain under the operator's `_local`/`_artifacts`
boundary.

## Boundaries

- PostgreSQL is the source of truth; Qdrant and Neo4j are rebuildable projections.
- Missing coverage, ambiguity, or store failures remain `partial` or `failed`.
- Contract fields such as `production` and `production_release` keep their
  fail-closed diagnostic meaning even though this repository has no production
  deployment path.
- See [ARCHITECTURE.md](ARCHITECTURE.md) and [docs/SECURITY.md](docs/SECURITY.md)
  for the data and security invariants.
