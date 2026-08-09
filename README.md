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

## Official trade-guidance snapshot boundary

`functional-uat-kr-trade` is a separate ignored local profile for a broader
Korean trade **guidance-only** snapshot. It has different local store ports,
passwords, token, and state directory from `functional-uat-3law`; creating,
building, or inspecting it never adopts, resets, or deletes the existing
three-law UAT state.

```powershell
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py init
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py status
```

This profile does **not** collect external material automatically. An approved
operator first stages bytes already obtained from a registry-approved official
source family below the ignored profile, then explicitly builds a bounded
local projection. The input is UTF-8 source text plus the hash-bound metadata
below; it does not contain credentials and it is never committed to Git.

```powershell
@'
{
  "schema_version": "functional-uat-kr-trade-source-build-input/v1",
  "snapshot_id": "krtrade-20260805-example",
  "as_of": "2026-08-05",
  "source_receipts": [
    {
      "source_id": "kcs_customs_guidance",
      "source_uri": "https://www.customs.go.kr/kcs/cm/cntnts/cntntsView.do?cntntsId=817&mi=2819",
      "relative_path": "data/guidance-sources/20260805-example/official-source.txt",
      "sha256": "<64-char-sha256>",
      "collected_at": "2026-08-05T00:00:00+00:00",
      "topics": ["customs_clearance"],
      "directions": ["import"]
    }
  ],
  "guidance_records": [
    {
      "source_id": "kcs_customs_guidance",
      "topic": "customs_clearance",
      "direction": "import",
      "excerpt": "<the exact short excerpt present in official-source.txt>",
      "locator": "<optional local document location>"
    }
  ]
}
'@ | Set-Content -Encoding utf8 `
  ..\_local\profiles\functional-uat-kr-trade\data\guidance-sources\20260805-example\source-build.json

uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py build `
  --source-manifest data/guidance-sources/20260805-example/source-build.json
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py verify
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py serve-guidance
```

`build` accepts only registry source IDs marked
`requires_kr_trade_snapshot`, the **exact** corresponding registry
`official_uri` as the operator's `source_uri` attestation, profile-relative
local paths, matching SHA-256 values, declared registry topic/direction, and
excerpts that occur in the hash-verified source text. It writes an immutable
local manifest containing the source URI, collection time, `as_of`, hash,
topic/direction coverage, and derived record IDs. `verify` repeats these
checks before every readiness/query request. It does not download, parse
arbitrary URLs, start Docker, reset a volume, or copy raw material into Git.

`source_uri` records which registry-approved official page the operator used;
the build never infers a URL from raw HTML. `as_of` is the selected **knowledge
snapshot reference date**, not a certification that an agency guide had legal
effect on that date. Consumers must present it as a snapshot reference date,
retain the local collection timestamp for audit, and avoid presenting either
value as a legal-effectivity determination.

The guidance-query response additionally returns the checked-in registry hash
as `registry_sha256`, and each result returns its stable
`registry_source_id`. Those two fields let an Agent bind a derived record ID
and its displayed URI back to the safe registry manifest without receiving raw
source bytes or local receipt paths.

`status=source_build_required` means no build exists. The old
receipt-only command remains compatible and yields
`registered_not_indexed`, which cannot serve. Only `status=ready` may start
`serve-guidance`, which exposes `/internal/rag/v3/guidance/query` and nothing
else. In particular, this is **not** a PostgreSQL/Qdrant/Neo4j Evidence
runtime: document review `/retrieve`, model retrieval, and a complete broader
Functional-UAT remain unavailable until a separately implemented Data-store
build succeeds.

### V2 page-level Korean trade coverage

V2 is additive: V1/r3 and the `functional-uat-3law` profile are not migrated,
rewritten, or deleted. Its checked-in registry is
`law_repository/src/guidance_registry_v2.json`; the Git-safe consumer copy is
`contracts/evidence/v3/guidance_registry_v2.manifest.json`. V2 declares 11
topics, 18 closed sub-scopes, and 28 applicable `(topic, scope, direction)`
bundles. A declaration is not coverage: a bundle is `covered` only when every
listed page-level official source has a hash-verified receipt and at least one
matching excerpt record.

```powershell
# This never fetches: an operator explicitly stages exact allowlisted sources
# under the ignored profile first.
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py build-v2 `
  --source-manifest data/guidance-sources/<staging>/source-build-v2.json
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py verify-v2
uv run --locked --no-sync --group dev python tools/functional_uat_kr_trade.py serve-guidance-v2 `
  --host 127.0.0.1 --port 18094
```

V2 build input binds one `topic` plus `scope` on every source receipt and
guidance record. It rejects an unregistered URL, a changed byte hash, a record
outside its page's direction/scope, and an excerpt absent from the verified
source text. An official law is represented by its exact current-status page
and its exact body page: the status page must prove the effective date at
`as_of`; the body page inherits that date only through the explicit registry
link. Thus the service does not pretend that a title/status page is an article
excerpt. A PDF source retains the raw-PDF hash and also requires a separately
hash-bound local text projection for its quoted excerpt; neither raw file nor
projection is committed to Git.

The V2 server exposes only loopback
`POST /internal/rag/v3/guidance/query` at port `18094`, using the local token
in `_local/profiles/functional-uat-kr-trade/evidence-service-token` and
`X-Customs-Service-Mode: functional-uat`. Its authenticated readiness endpoint
is `GET /internal/rag/v3/health/ready`. It fails closed with `503` when the V2
active pointer, source bytes, hashes, statute-effectivity proof, or derived
coverage no longer validates. Consumers must additionally compare every query
response's `registry_version`, `registry_sha256`, `snapshot_id`, and `as_of`
to their expected V2 contract before using any citation.

`scope_hint` and response `guidance_scope` are V3 additive fields. V2 accepts
one preselected scope only; it never turns arbitrary question text into an
open web or store search. `covered` means the defined source bundle was
present, not that all legal exceptions, foreign-country rules, a final HS,
tax, approval, or real-time agency status has been determined.

## Boundaries

- Agent consumers use versioned Evidence HTTP schemas in `contracts/evidence/`;
  they never access Data stores directly.
- Git contains code, V3 contracts, bounded HSK/CCCT reference inputs, and
  tests only. Secrets, raw official responses, model binaries, Docker volumes,
  and UAT results remain outside Git.
- `dev-fixture` is the team default. `dev-store` is Data-only development.
  `functional-uat-3law` is the explicit real-data functional check.
- `functional-uat-kr-trade` can serve only a hash-verified, explicitly built
  local guidance snapshot. It is not a replacement for the three-store
  Evidence profile or a claim of complete Korean trade-law coverage.
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
