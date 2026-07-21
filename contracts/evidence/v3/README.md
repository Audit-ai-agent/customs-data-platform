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
- `release_provenance_v1.schema.json`: the complete, fail-closed Evidence v3
  release bundle contract;
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

## Exact release provenance gate

`release_provenance_v1.schema.json` is additive and does not change the
retrieval wire contract. `tools/verify_evidence_v3_release.py` currently has
two deliberately different outcomes:

- candidate structural validation checks one canonical manifest, exact tagged
  provider bytes, and all referenced evidence, but exits with the explicit
  non-release status `CANDIDATE_STRUCTURAL_VALID_ONLY_NOT_RELEASED`;
- Production admission is unconditionally blocked with
  `NOT_IMPLEMENTED_EXTERNAL_ATTESTATION`.

The structural candidate verifier checks the following bindings:

- an existing annotated tag object and its exact clean commit target;
- immutable workflow file bytes, SHA, run ID, and run attempt;
- the allowlisted production provider context and all V3 Schema hashes;
- an exact 58-law, HSK, and CCCT generation with one matching PostgreSQL,
  Qdrant, and Neo4j attestation, epoch, evaluation, and seal;
- a no-skip Production RAG Gold/report that passes the existing recomputed
  Structured and Hybrid thresholds;
- an approved requirement policy, image-pinned reviewer trust root, coverage
  matrix, and two distinct valid Ed25519 reviewer signatures;
- passing secret/dependency/SAST reports, image digest, SBOM, build
  provenance, tested rollback image/generation, and a real Agent HTTP
  readiness/retrieval conformance result bound to the consumer lock.

Missing, extra, expired, unreleased, non-HTTP, mock, unsigned, mismatched, or
modified evidence fails closed. These consistency checks are not an authority:
plain JSON fields such as `PASS`, `actual_http`, or `restore_tested` can never
grant a Production release. Production admission remains blocked until a
protected producer, signed raw execution evidence, an independently pinned
release-attestor trust root, and two distinct Ed25519 release signatures over
the complete evidence subject are implemented. The read-only
`.github/workflows/evidence-v3-release.yml` downloads an externally produced
immutable bundle, validates its API-reported artifact digest, and invokes the
fail-closed verifier; it never creates or moves a tag, publishes an image,
promotes a generation, deploys, or mutates a database. The allowlisted
producer workflow is present but intentionally exits unsuccessfully and emits
no bundle.

The repository intentionally contains no complete Production release bundle,
and its policy/trust locks remain `unreleased`. Therefore the release workflow
cannot currently succeed. Synthetic bundles—including bundles signed with the
public deterministic test keys—can only exercise candidate structural checks
and are rejected by the Production entry point.
