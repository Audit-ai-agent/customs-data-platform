# Evidence V3

These are the only cross-repository Data contracts.

- `rag_retrieval_v3.schema.json` defines the request and response body for
  `POST /internal/rag/v3/retrieve`.
- `readiness_v3.schema.json` defines the response body for
  `GET /internal/rag/v3/health/ready`.

Local profiles require a loopback bearer token and
`X-Customs-Service-Mode`. Readiness is the minimal operational response
`{"status":"ready"}`; the response mode header is authoritative. The
functional-UAT manifest remains a local diagnostic artifact, not an HTTP
release/provenance, approval, trust, deployment-admission, or quality claim.
An unmapped requirement is always `human_only` and cannot enable automatic
completion.
