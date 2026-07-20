# Evidence Runtime Readiness v2

`readiness_v2.schema.json` is the canonical, byte-stable contract for the
authenticated production readiness endpoint:

```text
GET /internal/rag/v2/health/ready
```

The retrieval request/response body remains the frozen v1.0 schema. Runtime
v2 adds an immutable provider-code plus knowledge-generation release identity
and binds every retrieval with exactly one copy of these headers:

```text
X-Evidence-Schema-SHA256
X-Evidence-Provider-Release
X-Evidence-Provider-Revision
X-Evidence-Provider-Build-SHA256
X-Evidence-Knowledge-Generation
X-Evidence-Generation-Attestation-SHA256
X-Evidence-Evaluation-SHA256
X-Evidence-Runtime-Release-SHA256
```

The production application exposes no v1 retrieval route. Legacy v1 remains
available only through the explicit demo and test factories.
