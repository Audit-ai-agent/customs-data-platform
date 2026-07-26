# ADR-0001: Separate evidence retrieval from requirement verification authority

- Status: accepted for production candidate
- Scope: Evidence runtime v3
- Production approval: blocked pending two independent domain reviewers

## Context

Evidence v1/v2 can return a required-document code from CCCT data, but the mere
presence of that code does not establish that an uploaded certificate is
authentic, current, applicable to every item, or content-consistent. Treating
metadata presence as verification could create an unsafe automatic completion.

## Options and criteria

The options were: infer verification from the uploaded code, let Claude decide,
or publish a separately approved deterministic policy. The deciding criteria
were evidence traceability, reproducibility, fail-closed behavior, independent
legal review, and the ability to revoke a policy without rebuilding knowledge
data.

## Decision

Evidence v3 projects retrieval results through a closed verification policy.
Claude cannot author or modify this policy. The policy is a release input. A
canonical semantic approval subject binds the policy release identifier,
policy-manifest hash, coverage-matrix hash, knowledge source manifest/snapshot,
two independent review-reference hashes, adjudication hash, validity window,
and reviewer trust-store identity. Reviewers sign the domain-separated bytes
`customs-evidence/requirement-policy-approval/v1 || NUL || subject_sha256` with two
distinct Ed25519 keys. The complete policy file is separately revalidated and
its canonical approval hash is included in the runtime identity; the signature
does not claim to cover the raw JSON file bytes or insignificant formatting.
Reviewer public keys are not secret, but the exact trust-store hash is pinned
by an immutable provider-image input. Key IDs, public-key material, reviewer
identities, review references, and signatures must be independent; revoked or
historical key material cannot be reintroduced under another alias.
Cryptographic key uniqueness cannot prove that two keys are controlled by two
different people. Before activation, the organization must verify each
reviewer's real identity, domain role, independence, and private-key custody,
then bind the two onboarding-evidence hashes through the adjudication artifact
referenced by the signed approval subject. Missing identity-proof or key-custody
evidence is therefore a governance Release blocker, not a condition that code
may infer from reviewer ID strings.

The repository contains only an `unreleased` sentinel. Production receives an
approved policy and the image-pinned reviewer store through separate read-only
mounts. Missing, expired, malformed, unapproved, self-asserted, unknown-key,
revoked-key, invalid-signature, or changed data makes readiness fail. Unknown
requirement codes resolve to `human_only` rather than a permissive default.

## Consequences

- Runtime v2 remains immutable and available for rollback.
- Runtime v3 cannot become ready merely because all 58 target-law IDs exist.
- The legal coverage matrix and requirement-verification policy need two
  independent domain reviewers and adjudication before promotion.
- Until that external condition is met, the correct assurance level is
  `production_candidate` and Agent automatic completion remains disabled.
- A new approval can be rolled out independently, but Agent and Data must bind
  the same runtime-v3 digest for every request.

## Verification

Contract-drift tests compare checked-in Schema bytes with runtime models.
Runtime tests cover exact headers, missing identity, trust-root tampering,
signature/identity failures, approved policy projection, and fail-closed
readiness for unreleased sentinels. A loopback-only V3 conformance runner proves
an actual HTTP GET/17-header POST while remaining non-release and DB-free. Live release still
requires sealed three-store generation, RAG evaluation, rollback, and domain
approval evidence.
