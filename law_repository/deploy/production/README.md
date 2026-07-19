# Evidence API production deployment

This directory is the production shell for the versioned Agent-to-Data
Evidence boundary. It does not deploy or migrate PostgreSQL, Qdrant, or Neo4j,
and it does not expose the browser demo.

Do not deploy `src.webapp:app` or `create_evidence_application()` directly.
Those remain demo and contract-test boundaries. The supported entrypoint is:

```text
python -m src.production
```

## Release build

Build from one reviewed, clean commit. The context builder copies a committed
allowlist, writes `source-manifest.json` and `provider-build.json`, and refuses
a dirty repository or an output directory inside the repository.

```bash
python tools/build_production_context.py --output /secure-build/evidence-context
docker build \
  --file /secure-build/evidence-context/Dockerfile \
  --build-arg UV_IMAGE='ghcr.io/astral-sh/uv@sha256:<approved-digest>' \
  --build-arg PYTHON_IMAGE='python@sha256:<approved-3.11-slim-digest>' \
  --secret id=HF_TOKEN,src=/run/secrets/hf_read_token \
  --label org.opencontainers.image.revision="$(git rev-parse HEAD)" \
  --tag registry.example/evidence:<release> \
  /secure-build/evidence-context
```

`<approved-digest>` is an operator-supplied value from the approved registry;
the repository does not invent or float it. The build downloads the exact
model revisions declared in `src/embed/model_identity.py`, then makes model
loading offline-only. Never pass a secret as a Docker build argument.

Before publication, CI must:

1. scan the full Git history for secrets;
2. audit the locked Python environment;
3. scan the image and Docker/Compose configuration;
4. generate CycloneDX or SPDX SBOMs for the Python environment and image;
5. sign the image digest and attach build provenance;
6. verify the image as UID 65532 with read-only rootfs and no Linux capabilities.

The release manifest records the Data commit, image digest, provider build SHA,
retrieval schema SHA, model revisions, SBOM digests, scanner results, and
rollback image digest. A tag by itself is not a release identity.

## Transport and authentication

TLS 1.2 or newer is mandatory at the approved ingress or service mesh. The
application is not published on a host port and does not create certificates.
The ingress must:

- own certificates from the approved CA and validate the requested host;
- remove inbound `Forwarded` and `X-Forwarded-*` fields;
- set exactly one `X-Forwarded-Proto: https`;
- connect from an exact CIDR listed in `EVIDENCE_TRUSTED_PROXY_CIDRS`;
- reject ambiguous `Content-Length`/`Transfer-Encoding` framing before proxying;
- enforce a request-body limit no larger than the application limit;
- preserve `X-Request-ID` only when it matches the documented safe syntax.

The service rejects an untrusted proxy, duplicate forwarded protocol, or
non-HTTPS assertion. Do not use `0.0.0.0/0`, `::/0`, a query token, the demo
token, or provider build hashes as authentication.

Every `/internal/rag/*` endpoint except `/internal/rag/v1/health/live` requires
exactly one constant-time compared `Authorization: Bearer <service-token>`.
The token must contain 32-512 bearer-safe ASCII characters and should be a
short-lived, randomly generated service credential delivered as a mounted
secret file. Readiness and metrics are authenticated.

Verification from a trusted client:

```bash
curl --fail --cacert /approved/ca.pem \
  https://evidence.example/internal/rag/v1/health/live

curl --fail --cacert /approved/ca.pem \
  -H "Authorization: Bearer $EVIDENCE_SERVICE_TOKEN" \
  https://evidence.example/internal/rag/v1/health/ready
```

Also verify the negative cases: HTTP, invalid CA/host, direct container access,
missing/duplicate/wrong bearer, query token, duplicate build header, oversized
body, and `Content-Length` plus `Transfer-Encoding` must all fail before Data
retrieval.

## Runtime configuration

Service and Data-store passwords are mounted as files. Production refuses
`change_me`, an empty PostgreSQL/Neo4j password, remote PostgreSQL without a TLS
sslmode, remote Qdrant without HTTPS and an API key, and Neo4j without a `+s`
encrypted URI. The API database roles must be read-only and must not own
migrations.

The only cleartext exception is explicit local development:

```text
EVIDENCE_TRANSPORT_MODE=loopback_dev
EVIDENCE_ALLOW_INSECURE_LOOPBACK_DEV=true
EVIDENCE_BIND_HOST=127.0.0.1
```

All three Data stores must also resolve to loopback. This mode is never a
release environment.

## Limits, lifecycle, and observability

- JSON is read incrementally and capped before FastAPI parsing.
- Duplicate JSON keys, NaN/Infinity, compression, unsafe media types,
  duplicate security headers, and request-ID mismatch fail closed.
- Admission has a fixed concurrent-work limit and no unbounded application
  queue. Saturation returns `503` and `Retry-After`.
- A deadline returns `504`; the occupied admission slot remains held until the
  underlying thread actually finishes, preventing timeout-driven thread growth.
- Shutdown stops admission and drains admitted work for the configured grace
  period.
- Responses are buffered within a fixed byte budget and carry `no-store`,
  `nosniff`, and a service request ID.
- Logs contain only bounded route names, method, status, latency, request ID,
  and exception type. They never contain headers, bearer values, query text,
  document descriptions, raw responses, or DB exception strings.
- Authenticated Prometheus text metrics are available at
  `/internal/rag/v1/metrics`; labels are fixed and contain no case/item data.

Alert on sustained 401/412/413/429-or-503/504 rates, concurrency saturation,
readiness failure, latency, process restart, certificate/token expiry, and
provider/generation mismatch. Logs and metrics are operations evidence, not a
store for trade documents.

## Rollout and rollback

1. Verify the new image signature, SBOM, source/provider manifests, and exact
   Data generation in a non-production environment.
2. Add a new replica behind the ingress and require liveness, authenticated
   readiness, and a pinned Agent conformance request to pass.
3. Drain old replicas; do not move an existing release tag.
4. On failure, remove the new digest and restore the prior signed image digest.
   Data generation rollback is a separate operation and must use its own
   verified published-generation pointer.

Bearer rotation uses a staged rolling deployment: deploy a new service
credential according to the platform secret-rotation runbook, update the Agent
credential, verify conformance, then revoke the old credential. Do not attempt
an in-place token edit without a rollout, and never print either token while
verifying.
