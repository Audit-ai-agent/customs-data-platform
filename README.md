# Customs Data Platform

Korean customs-law ingestion, three local knowledge stores, and the typed
Evidence API consumed by `customs-ai-agent`. This repository owns the Evidence
schemas and Data-side development/UAT profiles; it does not publish an image,
cloud deployment, or release workflow.

## Team development

Clone this repository beside the other product repositories and the shared
`customs-engineering-harness`, then run the normal checks once:

```powershell
Set-Location ..\customs-engineering-harness
.\harness\eng.ps1 doctor
.\harness\eng.ps1 setup --repo data_platform
.\harness\eng.ps1 check quick --repo data_platform
```

The default full-stack Data dependency is a deterministic, loopback-only
fixture. It needs no Docker, model cache, Law API key, or Claude key.

```powershell
Set-Location ..\customs-data-platform
# The Harness setup command performs this once; direct use is identical.
uv sync --locked --group dev --python 3.11
uv run --locked --no-sync --group dev python tools/run_dev_fixture_evidence.py `
  --host 127.0.0.1 --port 18083
```

The generated bearer token remains only in
`_local/profiles/dev-fixture/evidence-service-token`. The Agent sends it as
`Authorization: Bearer ...` with `X-Customs-Service-Mode: local-development`.
The fixture preserves the Evidence V3 request/response body but is synthetic,
human-review-only, and never a legal or release result.

## Optional Data-store development

Use real local PostgreSQL/Qdrant/Neo4j only while changing Data ingestion or
retrieval behavior. This profile has its own Docker project, ports, volumes,
and random local passwords, so it does not collide with functional UAT.

```powershell
uv run --locked --no-sync --group dev python tools/dev_store.py init
uv run --locked --no-sync --group dev python tools/dev_store.py compose up
uv sync --locked --group dev --extra store --extra ingestion --python 3.11
uv run --locked --no-sync --group dev --extra store --extra ingestion python tools/dev_store.py exec -- `
  python -m src.ingest.hsk
# Optional Data-maintainer check after a complete law/HSK/CCCT store build.
# This is the only command that compares every current law/index/graph identifier.
uv run --locked --no-sync --group dev --extra store python tools/dev_store.py verify
```

## Real functional UAT: three customs laws

`functional-uat-3law` verifies the actual local service path with the exact
three law IDs `001556`, `002421`, and `006392` as of `2026-08-05`. It is a
functional check, not Production approval: results remain human-review-only,
and there is no release admission, signature, trust store, quality gate, or
deployment step.

```powershell
uv sync --locked --group dev --extra store --extra rag --extra ingestion --python 3.11
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py init
# Add only your personal LAW_API_OC to the ignored _local profile config.
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py compose up
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py build --as-of 2026-08-05
# Optional after build, Data/schema/retrieval changes, or an unexpected result.
# It is intentionally not part of each functional UAT run.
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py verify-store
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py serve `
  --host 127.0.0.1 --port 18083
```

To run a new three-law build on the same workstation, reset only this profile
explicitly after the prior local run has finished:

```powershell
uv run --locked --no-sync --group dev --extra store --extra rag --extra ingestion python tools/functional_uat_3law.py reset `
  --confirm-profile functional-uat-3law
```

This stops and removes only the `customs-functional-uat-3law` Docker project's
volumes and its active build marker. It does not touch any other Docker
project, legacy state, raw local law sources, or `_artifacts` results.

The build downloads the pinned BGE models into `_local/cache/huggingface`,
writes official raw law source only below
`_local/profiles/functional-uat-3law/data/laws/`, and writes a redacted manifest
containing only profile, date, law IDs, source hashes, model revisions, store
counts, and build time to `_artifacts/functional-uat/<run-id>/manifest.json`.
The server checks local source hashes, model revisions/cache, and lightweight
three-store counts before it starts. Run `verify-store` only when a Data build
or retrieval change needs the full PostgreSQL/Qdrant/Neo4j ID and hash comparison.
`LAW_API_OC` is loaded only while `build` collects the three laws; the
long-running `serve` process removes it from its environment and settings.
Its Agent-facing mode is `functional-uat`. The bounded CCCT reference
input is already versioned in this repository, so no data.go.kr key, bulk
collection, or external refresh is part of team development or UAT.

## Boundaries

- Agent consumers use versioned Evidence HTTP schemas in `contracts/evidence/`;
  they never access Data stores directly.
- Git contains code, V3 contracts, bounded HSK/CCCT reference inputs, and
  tests only. Secrets, raw official responses, model binaries, Docker volumes,
  and UAT results remain outside Git.
- `dev-fixture` is the team default. `dev-store` is Data-only development.
  `functional-uat-3law` is the explicit real-data functional check.
- A pre-cleanup local volume is not adopted by the current migration runner.
  Reset/rebuild that profile instead of trying to reuse it.

## Local checks

`setup` is the only step that installs or reuses dependencies. The following
commands are deliberately `--no-sync`, so checks do not install, delete, or
clean caches:

```powershell
uv run --locked --no-sync --group dev python tools/check_contract_drift.py
uv run --locked --no-sync --group dev ruff format --check law_repository/src law_repository/tests tools
uv run --locked --no-sync --group dev ruff check law_repository/src law_repository/tests tools
uv run --locked --no-sync --group dev mypy
uv run --locked --no-sync --group dev pytest -q
```
