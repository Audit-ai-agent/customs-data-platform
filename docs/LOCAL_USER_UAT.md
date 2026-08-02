# LOCAL USER UAT Evidence profiles

This document describes non-release Evidence processes used only by the local
user workbench. They do not replace the sealed Production V3 entrypoint.

## `local-user-uat` synthetic V3

`tools/run_evidence_v3_conformance_server.py --profile local-user-uat` starts
an authenticated, literal-loopback V3 HTTP service. It uses the real V3 wire,
identity binding, request correlation, and policy projection, but it never
opens PostgreSQL, Qdrant, or Neo4j and is not RAG-quality evidence.

The profile provides two explicit controls:

- `7318151000`: covered import query with no required document. This is the
  clean user-flow Positive Control.
- `0303540000`: covered import query requiring the synthetic
  `TEST_ONLY_IMPORT_CONFIRMATION` document. This validates the safe
  untrusted-supporting-document path.

The server writes its exact V3 identity outside the immutable build context.
The local Agent manifest must embed those exact bytes. The service exposes
authenticated `/internal/rag/v3/health/uat-ready`; all responses state that
release readiness, RAG evaluation, and automatic completion are false.

## `preserved-3law-v3-diagnostic` actual-store V3

`tools/run_three_law_v3_diagnostic.py` is the only supported bridge from the
current Agent V3 UAT boundary to the preserved real three-law stores. It
reuses, rather than replaces, the reviewed
`tools/run_three_law_v2_diagnostic.py` observation and retrieval backend.

Startup fails closed unless all of the following are simultaneously true:

- the process is bound to literal `127.0.0.1` or `::1`;
- the executing Data checkout is clean and equals the provider revision;
- PostgreSQL contains exactly law IDs `001556`, `002421`, and `006392`;
- PostgreSQL has exactly 1,007 eligible current article leaves;
- Qdrant has exactly the same 1,007 point identities and complete
  dense/BM25 retrieval state;
- Neo4j has exactly the same 1,007 article identities and complete graph
  retrieval state;
- the CCCT snapshot and all retrieval-affecting store hashes remain unchanged
  before and after every readiness/retrieval operation;
- PostgreSQL enforces read-only transactions.

The bridge exposes the canonical V3 readiness/retrieval paths and all 17 V3
identity headers. An additional authenticated
`/internal/rag/v3/health/diagnostic-ready` endpoint reports only non-secret
diagnostic provenance: profile, 3/58 scope, cross-store counts, snapshot, and
the exact V3 identity.

The V3 policy envelope is a public, deterministic test-only fixture whose
single reserved document code can never approve a real CCCT requirement.
Every real requirement therefore resolves to `human_only`, makes V3 coverage
`partial`, and keeps the Agent in human review. This is intentional: successful
RAG retrieval must not be confused with approved requirement verification.

Run it only from an exact committed Data revision and keep all generated
identity/manifest/token files outside the repository and provider context:

```powershell
uv run --locked --extra rag python tools/build_production_context.py `
  --repository . `
  --output <EMPTY_EXTERNAL_PROVIDER_CONTEXT>

uv run --locked --extra rag python tools/run_three_law_v3_diagnostic.py `
  --diagnostic-only-three-law `
  --provider-context <EXTERNAL_PROVIDER_CONTEXT> `
  --service-token-file <ABSOLUTE_EXTERNAL_TOKEN_FILE> `
  --identity-output <NEW_EXTERNAL_V3_IDENTITY_JSON> `
  --diagnostic-manifest-output <NEW_EXTERNAL_DIAGNOSTIC_MANIFEST_JSON> `
  --host 127.0.0.1 `
  --port 18013
```

This profile proves actual Structured lookup, BGE-M3 dense retrieval, BM25
sparse retrieval, RRF/rerank, and Neo4j expansion when the request exercises
those paths. It still reports:

```text
scope=DIAGNOSTIC_ONLY_3_OF_58
release_ready=false
rag_quality_evaluated=false
domain_reviewed=false
automatic_completion_eligible=false
```

`rag_quality_evaluated=false` means that one successful live retrieval is not
a ranking-quality evaluation. A separately reviewed three-law Gold run may
report diagnostic Hit@5/Recall@10/nDCG@10, but it cannot change this runtime
identity or satisfy the 58-law Production Release Gate.

With the V3 diagnostic server still running, execute the restricted five-case
Hybrid Gold and HSK `0303540000` Structured check as follows:

```powershell
uv run --locked --extra rag --extra ingestion python tools/run_three_law_v3_restricted_eval.py `
  --diagnostic-only-three-law `
  --evidence-base-url http://127.0.0.1:18013 `
  --service-token-file <ABSOLUTE_EXTERNAL_TOKEN_FILE> `
  --identity-file <EXTERNAL_V3_IDENTITY_JSON> `
  --review-as-of <SUPPORTED_REVIEW_AS_OF> `
  --output <NEW_EXTERNAL_RESTRICTED_EVAL_JSON>
```

The preserved diagnostic performs a complete read-only three-store attestation
before and after each request. Its server-side request deadline is therefore
180 seconds. The evaluator uses separate
`--readiness-timeout-seconds` and `--retrieval-timeout-seconds` values, each
defaulting to 195 seconds so the server can fail closed before the client
disconnects. Both client timeout values are bounded to at most 300 seconds.
If evaluation fails, the success output remains absent and a sibling
`<OUTPUT_STEM>.failure.json` records only the failure stage/code, completed
case counts, and monotonic durations. It never records the query, request body,
service location, token, or raw response.

The runner sends all requests over authenticated loopback HTTP with the exact
17 binding headers and verifies both standard and diagnostic readiness
identities. For Hybrid ranking only, it projects the V3 response back to the
V1 retrieval result by removing solely the expected
`REQUIREMENT_POLICY_UNAVAILABLE` overlay; it never adds or reorders Evidence.
The report keeps that V3 policy partial state separately. It also counts
`retrieval_methods` and `raw_scores` on returned **law article** Evidence and
fails unless `dense`, `bm25`, `graph`, `rrf`, and `lexical_overlap` were
actually observed. Structured requirement `exact_hs/graph` evidence cannot
substitute for the law-graph check. The report is always marked:

```text
mode=THREE_LAW_RESTRICTED_RAG_DIAGNOSTIC
production_claim=false
production_rag_quality_evaluated=false
```

The current five-case Hybrid Gold references law IDs `001556` and `002421`
only. Law `006392` is attested by the three-store runtime preflight but has no
ranking label in this Gold. Every report therefore exposes
`loaded_law_ids`, `hybrid_gold_relevant_law_ids`,
`hybrid_gold_law_coverage=2/3`, and
`all_three_laws_hybrid_gold_covered=false`. Passing the restricted Gate must
never be described as ranking validation of all three loaded laws.

## `preserved-3law-v3-scenario-eval` actual-store UAT

`tools/run_three_law_v3_scenario_eval.py` is the local user-scenario profile.
It is separate from, and does not change,
`preserved-3law-v3-diagnostic`. Both profiles read the same preserved
PostgreSQL/Qdrant/Neo4j knowledge snapshot, but their V3 policy identities are
deliberately different.

Scenario-eval startup fails closed unless the normal actual-store attestation
passes and all of these additional facts are true:

- exactly 3 laws and exactly 1,007 PostgreSQL leaves, Qdrant points, and
  Neo4j Article identities are unchanged;
- HSK `0303540000` resolves exactly in the preserved graph;
- its current read-only Structured import lookup contains
  `CCCT_DOC_46E819BB87B27371`;
- a second complete three-store observation after that lookup equals the
  first observation;
- the signed UAT fixture contains exactly that one document-code policy;
- that binding is `approved=true` and `verification_policy=human_only`;
- all generated outputs are new files outside both the Data checkout and the
  immutable provider context;
- the service binds to literal loopback only.

The fixture approval proves only that two public test keys signed the exact
fixture. It is not a customs-domain or legal approval. Its private keys and
policy builder remain outside `law_repository/src` and the Production image
allowlist. Unknown or different document codes continue to resolve to the
normal unapproved `human_only` fallback. The sealed fixture uses fixed approval
and key material valid from `2026-07-24` through `2027-07-24`, inclusive. Its
policy/approval/trust hashes therefore remain reproducible throughout that
window; normal verification fails before approval and after expiry. Extending
the window requires an explicit new reviewed fixture release rather than
changing `.v1` in place.

Build a provider context from the exact clean committed Data revision, then
start the service:

```powershell
uv run --locked --extra rag python tools/build_production_context.py `
  --repository . `
  --output <EMPTY_EXTERNAL_PROVIDER_CONTEXT>

uv run --locked --extra rag python tools/run_three_law_v3_scenario_eval.py `
  --scenario-eval-only-three-law `
  --provider-context <EXTERNAL_PROVIDER_CONTEXT> `
  --service-token-file <ABSOLUTE_EXTERNAL_TOKEN_FILE> `
  --identity-output <NEW_EXTERNAL_V3_IDENTITY_JSON> `
  --scenario-manifest-output <NEW_EXTERNAL_SCENARIO_MANIFEST_JSON> `
  --trace-output <NEW_EXTERNAL_EVIDENCE_TRACE_JSONL> `
  --host 127.0.0.1 `
  --port 18014
```

The authenticated additional readiness path is
`/internal/rag/v3/health/scenario-eval-ready`. Both that response and the
external manifest expose these exact composition fields:

```text
evidence_profile=preserved-3law-v3-scenario-eval
evidence_scope=ACTUAL_3_LAW_RAG_SCENARIO_EVAL_3_OF_58
evidence_data_source=ACTUAL_POSTGRESQL_QDRANT_NEO4J_READ_ONLY
evidence_policy_mode=SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY
```

The scenario readiness also returns a credential-free `store_state` projection:
the exact PostgreSQL article, Qdrant point, and Neo4j article counts; each
store's retrieval-state SHA-256; the Knowledge snapshot; and one canonical
combined SHA-256. The regular V3 readiness check re-observes all three stores
immediately before this projection is returned and responds `503` if it differs
from the sealed startup observation. A UAT harness can therefore compare the
start and end projections without connecting directly to any Data database.

The standard V3 request body and response remain unchanged. The V3 identity
continues to expose exactly 17 binding headers, including the distinct policy
release, policy manifest, approval, reviewer trust-store, and final V3 runtime
hashes. Missing or mismatched identity headers return `412`; timeout, policy
drift, or Store drift fails closed and never selects a synthetic backend.

The trace output is an append-only
`three-law-evidence-v3-scenario-eval-trace/v1` JSONL stream. One row per V3
retrieval request contains only:

- caller request ID and HTTP status;
- fixed profile/scope/data-source/policy-mode values;
- knowledge snapshot and safe provider/policy identity hashes;
- Structured, Dense, BM25, and Graph attempted/succeeded flags, aggregate
  result counts, Top-K inclusion counts, and elapsed milliseconds.

It never stores a query, request/response body, document text, HSK input,
source text, token, credential, service location, or raw provider response.
`graph.succeeded=true` means the graph execution path completed;
`graph.top_k_result_count>0` separately proves Graph Evidence survived into
the returned Top-K.

Every readiness and retrieval performs a full state observation before use
and another equality check before accepting the result. PostgreSQL
transactions are forced read-only, no generation is promoted, and all
requests remain:

```text
release_ready=false
rag_quality_evaluated=false
domain_reviewed=false
automatic_completion_eligible=false
```

## Preserved V2 history

`tools/run_three_law_v2_diagnostic.py` remains preserved for TASK-0018
reproducibility and direct V2 comparison. New Agent V3 UAT composition must use
one of the explicit V3 profiles above rather than downgrading the Agent
consumer to V2.

## Boundary rules

- Never set a Production release lock to these identities.
- Never publish any diagnostic service outside loopback.
- Never use a test reviewer key or `test_issuer` as Production trust.
- Never report `auto_review_complete` from these profiles.
- Never lower 3/58 or 1,007-count checks to make a changed store appear ready.
- Never label one successful three-law retrieval as Production RAG quality.
- Never reuse the scenario-eval fixture signature as legal or Production
  approval.
- Store bearer tokens outside Git and pass them through regular files.
