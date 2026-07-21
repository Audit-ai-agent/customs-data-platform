# Evidence runtime v3

Evidence v3 is an additive production-candidate contract. It keeps the sealed
three-store generation identity from runtime v2 and adds a separately reviewed
requirement-document verification policy release.

The directory contains provider-owned canonical bytes:

- `rag_retrieval_v3.schema.json`: v3 request/response wire contract;
- `readiness_v3.schema.json`: readiness and complete runtime identity;
- `requirement_policy_v1.schema.json`: externally approved policy release;
- `trusted_reviewer_store_v1.schema.json`: allowlisted domain-reviewer keys;
- `trusted_reviewer_lock_v1.schema.json`: immutable image trust-root lock;
- `requirement-policy.lock.json`: an intentionally **unreleased** repository
  sentinel, never a production approval;
- `trusted-reviewers.lock.json`: an intentionally **unreleased** trust-root
  sentinel containing no production public keys.

Production does not infer certificate verification from an uploaded document
code. Every returned requirement is bound to one of the closed policies:
`presence_only`, `content_match_required`,
`external_verification_required`, or `human_only`. An unmapped or unapproved
requirement is always `human_only`.

An approved policy requires two different allowlisted domain reviewers, two
different Ed25519 public keys, two review references, and two signatures. They
sign the same canonical semantic, domain-separated approval-subject hash. That subject binds the review
references, adjudication, validity period, coverage matrix, policy manifest,
and exact knowledge source-manifest/snapshot. Public keys come from a separate
read-only reviewer store whose canonical SHA-256 is pre-pinned in the provider
image. Replacing both runtime mounts cannot establish a new trust root.

The policy and reviewer store are mounted at
`/run/evidence-release/requirement-policy.json` and
`/run/evidence-release/trusted-reviewers.json`. Unknown, duplicate, revoked,
inactive, public-key aliases (including revoked/history aliases), reused
signatures, or invalidly signed reviewers fail closed. The checked-in unreleased
sentinels cannot make v3 ready.

The readiness identity binds the exact v3 retrieval/readiness/policy Schema
bytes, verified policy approval, reviewer trust-store hash, coverage matrix,
and the complete v2 provider,
knowledge-generation, evaluation, and model identity. Retrieval repeats the
same 17-field identity in headers and fails with `412` before search on any
mismatch.

Changing v1 or v2 in place is prohibited. A breaking v3 change requires a new
contract directory and release.
