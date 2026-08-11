# Evidence V3

These are the only cross-repository Data contracts.

- `rag_retrieval_v3.schema.json` defines the request and response body for
  `POST /internal/rag/v3/retrieve`.
- `readiness_v3.schema.json` defines the response body for
  `GET /internal/rag/v3/health/ready`.
- `guidance_query_v3.schema.json` defines the request and response body for
  `POST /internal/rag/v3/guidance/query`.
- `guidance_registry_v1.manifest.json` is the provider-owned, safe topic and
  source-scope manifest that consumers use to understand the guidance surface.

Local profiles require a loopback bearer token and
`X-Customs-Service-Mode`. Readiness is the minimal operational response
`{"status":"ready"}`; the response mode header is authoritative. The
functional-UAT manifest remains a local diagnostic artifact, not an HTTP
release/provenance, approval, trust, deployment-admission, or quality claim.
An unmapped requirement is always `human_only` and cannot enable automatic
completion.

The guidance query is separate from commodity/HS requirement retrieval. It
accepts a bounded natural-language query and may include an optional exact
registry `topic_hint` selected by the Agent. A hint is an enum value, not free
text; when it is present Data does not reclassify the raw wording. Otherwise
Data maps the question to one versioned registry topic and searches only
registry-owned terms. It never acts as an open law-search endpoint.
`guidance_registry_v1.manifest.json` declares the source ID, official URI,
authority, direction, topic, and local-build availability for each source
family. It does not contain raw material and a declared source is not
automatically a collected source.

Every guidance response carries `registry_sha256`, the exact hash of that
provider-owned manifest. Each returned `GuidanceSourceV3` also carries
`registry_source_id`, the stable registry source-family ID behind its
hash-derived result ID. Consumers must bind the pair to this manifest before
presenting a URI as an official source. A `development_fixture` result keeps
its synthetic kind; its `registry_source_id` identifies only the deterministic
topic/direction scope, not a collected official source.

`GuidanceSourceV3.source_kind` keeps official provenance explicit:
`official_law`, `official_law_catalog`, `official_agency_guidance`,
`official_requirement_reference`, `official_public_guidance`, or the clearly
synthetic `development_fixture`. Only `official_law` carries law/article
fields; the other official source kinds retain their registry source family
and official URI without pretending to be a law article.

`local-development` returns a clearly marked deterministic fixture source.
The default `functional-uat-law-history` `/retrieve` profile validates its
active local receipts and temporal stores before each authenticated retrieval.
Its additive `temporal_context` contains `law_as_of`,
`hsk_snapshot_as_of`, and `requirements_snapshot_as_of`; all three are
required when the object is present. Its additive `temporal_coverage` labels
each `law`, `hsk`, and `requirements` component as `covered` or
`unavailable`; consumers must use it with the dates and must never turn an
unavailable baseline date into same-date evidence. A verified, explicitly
activated official HSK/CCCT archive may cover its exact date. Any other legal
date remains `partial`, never a hidden complete result.
The retained `functional-uat-3law` response may omit this object for legacy
compatibility. The legacy launcher still validates the active three-law source
scope before every authenticated guidance request and returns only sources
from that locally validated corpus. The separate
`functional-uat-kr-trade` profile is `source_build_required` until an explicit
allowlisted local source build succeeds. Each source-build receipt must attest
the exact registry `official_uri`; the immutable local manifest preserves that
URI, its local collection time, and the source hash. A verified build may serve
**only** this guidance-query endpoint: it validates local source hashes and
requires every returned excerpt to occur in the staged source. It is not a substitute
for the full three-store `/retrieve` runtime and must not claim broad Korean
trade coverage. `no_coverage` means that the active local snapshot cannot
support the requested topic; it does not mean that no legal rule exists.

The response `as_of` identifies the selected knowledge-snapshot reference
date. It is not, by itself, a legal-effective-date assertion for a non-law
agency guide; user interfaces must label it accordingly and must not infer one.
