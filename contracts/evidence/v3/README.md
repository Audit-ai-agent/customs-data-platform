# Evidence runtime v3

Evidence v3 is an additive runtime contract. It preserves the sealed
three-store identity from v2 and adds a separately reviewed requirement
verification policy binding.

Provider-owned canonical bytes:

- `rag_retrieval_v3.schema.json`: request/response wire contract;
- `readiness_v3.schema.json`: complete runtime identity;
- `requirement_policy_v1.schema.json`: signed requirement-policy format;
- `trusted_reviewer_store_v1.schema.json`: allowlisted reviewer keys;
- `trusted_reviewer_lock_v1.schema.json`: immutable trust-store lock;
- `requirement-policy.lock.json` and `trusted-reviewers.lock.json`: intentionally
  unreleased local sentinels, never an approval.

An unmapped or unapproved requirement remains `human_only`. The readiness
identity binds the exact v3 schemas, policy and reviewer-store hashes, and the
v2 provider/knowledge-generation/model identity. Retrieval repeats the same
bindings in headers and fails closed on mismatch.

The repository does not publish a release bundle, image, deployment recipe, or
production admission workflow. Authorized local UAT receives its operator
inputs outside Git; normal team development uses the versioned schemas and
offline conformance checks only.
