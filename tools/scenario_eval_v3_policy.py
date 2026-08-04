"""Signed, test-only policy for the preserved three-law scenario evaluation.

The material in this module is a public UAT fixture.  Its deterministic private
keys are intentionally recoverable and therefore have no legal, domain-review,
deployment, or Production authority.  The module lives outside
``law_repository/src`` and is not part of the provider-context source allowlist.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from src.requirement_policy_release import (
    RequirementPolicyApproval,
    RequirementPolicyEntry,
    RequirementPolicyRelease,
    ReviewerAttestation,
    TrustedReviewerKey,
    TrustedReviewerStore,
    TrustedReviewerStoreLock,
    VerifiedRequirementPolicyRelease,
    approval_sha256,
    approval_signature_message,
    policy_approval_subject_sha256,
    policy_manifest_sha256,
    trusted_reviewer_store_sha256,
    verify_requirement_policy_release,
)

SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE = "CCCT_DOC_46E819BB87B27371"
SCENARIO_EVAL_POLICY_MODE = "SEALED_TEST_ONLY_ACTUAL_CCCT_HUMAN_ONLY"
SCENARIO_EVAL_POLICY_RELEASE = "test-only-three-law-scenario-eval-human-only.v1"
SCENARIO_EVAL_TRUST_RELEASE = "test-only-three-law-scenario-eval-trust.v1"
SCENARIO_EVAL_POLICY_APPROVED_AT = date(2026, 7, 24)
SCENARIO_EVAL_POLICY_VALID_FROM = date(2026, 7, 24)
SCENARIO_EVAL_POLICY_VALID_UNTIL = date(2027, 7, 24)

_PRIVATE_KEY_DOMAIN = (
    b"customs-data-platform/three-law-scenario-eval/public-test-private-key/NON-PRODUCTION\x00"
)


@dataclass(frozen=True, slots=True)
class ScenarioEvalPolicyFixture:
    """Verified UAT fixture plus the public trust material used to verify it."""

    verified: VerifiedRequirementPolicyRelease
    trust_store: TrustedReviewerStore
    trust_store_lock: TrustedReviewerStoreLock


def _private_key(index: int) -> Ed25519PrivateKey:
    seed = hashlib.sha256(_PRIVATE_KEY_DOMAIN + str(index).encode("ascii")).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def _public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        .hex()
    )


def build_scenario_eval_policy_fixture(
    *,
    knowledge_source_manifest_sha256: str,
    knowledge_snapshot_id: str,
    today: date | None = None,
) -> ScenarioEvalPolicyFixture:
    """Build the reproducible, narrowly scoped ``human_only`` scenario policy.

    ``today`` selects only the verification date.  Approval, key validity, and
    signatures remain fixed so the same knowledge identity produces the same
    V3 policy identity throughout the explicit UAT validity window.  The
    regular verifier still rejects a build before approval or after expiry.
    """

    verification_date = today or date.today()
    private_keys = [_private_key(1), _private_key(2)]
    reviewer_claims = [
        {
            "reviewer_id": f"scenario-eval-test-reviewer-{index}",
            "key_id": f"scenario-eval-test-ed25519-key-{index}",
            "review_reference_sha256": hashlib.sha256(
                f"scenario-eval-test-review-reference-{index}".encode("ascii")
            ).hexdigest(),
        }
        for index in (1, 2)
    ]
    trust_store = TrustedReviewerStore(
        contract="evidence-domain-reviewer-trust-store",
        schema_version="1.0",
        trust_store_release=SCENARIO_EVAL_TRUST_RELEASE,
        valid_from=SCENARIO_EVAL_POLICY_VALID_FROM,
        valid_until=SCENARIO_EVAL_POLICY_VALID_UNTIL,
        keys=[
            TrustedReviewerKey(
                reviewer_id=claim["reviewer_id"],
                key_id=claim["key_id"],
                role="customs_domain_reviewer",
                algorithm="ed25519",
                public_key_hex=_public_key_hex(private_key),
                valid_from=SCENARIO_EVAL_POLICY_VALID_FROM,
                valid_until=SCENARIO_EVAL_POLICY_VALID_UNTIL,
                revoked=False,
            )
            for claim, private_key in zip(reviewer_claims, private_keys, strict=True)
        ],
    )
    trust_sha256 = trusted_reviewer_store_sha256(trust_store)
    entries = [
        RequirementPolicyEntry(
            required_document_code=SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE,
            verification_policy="human_only",
            policy_source_sha256=hashlib.sha256(
                b"three-law-scenario-eval/human-only-policy/NON-PRODUCTION"
            ).hexdigest(),
        )
    ]
    manifest_sha256 = policy_manifest_sha256(entries)
    adjudication_sha256 = hashlib.sha256(
        b"three-law-scenario-eval/test-adjudication/NON-PRODUCTION"
    ).hexdigest()
    coverage_sha256 = hashlib.sha256(
        b"three-law-scenario-eval/exact-one-code-coverage/NON-PRODUCTION"
    ).hexdigest()
    subject_sha256 = policy_approval_subject_sha256(
        policy_release=SCENARIO_EVAL_POLICY_RELEASE,
        knowledge_source_manifest_sha256=knowledge_source_manifest_sha256,
        knowledge_snapshot_id=knowledge_snapshot_id,
        coverage_matrix_sha256=coverage_sha256,
        policy_manifest_sha256=manifest_sha256,
        adjudication_sha256=adjudication_sha256,
        reviewer_claims=reviewer_claims,
        trust_store_release=trust_store.trust_store_release,
        reviewer_trust_store_sha256=trust_sha256,
        approved_at=SCENARIO_EVAL_POLICY_APPROVED_AT,
        valid_until=SCENARIO_EVAL_POLICY_VALID_UNTIL,
    )
    message = approval_signature_message(subject_sha256)
    approval = RequirementPolicyApproval(
        reviewer_attestations=[
            ReviewerAttestation(
                **claim,
                signature_hex=private_key.sign(message).hex(),
            )
            for claim, private_key in zip(reviewer_claims, private_keys, strict=True)
        ],
        adjudication_sha256=adjudication_sha256,
        subject_sha256=subject_sha256,
        trust_store_release=trust_store.trust_store_release,
        reviewer_trust_store_sha256=trust_sha256,
        approved_at=SCENARIO_EVAL_POLICY_APPROVED_AT,
        valid_until=SCENARIO_EVAL_POLICY_VALID_UNTIL,
    )
    release = RequirementPolicyRelease(
        contract="requirement-verification-policy",
        schema_version="1.0",
        release_state="approved",
        policy_release=SCENARIO_EVAL_POLICY_RELEASE,
        knowledge_source_manifest_sha256=knowledge_source_manifest_sha256,
        knowledge_snapshot_id=knowledge_snapshot_id,
        coverage_matrix_sha256=coverage_sha256,
        policy_manifest_sha256=manifest_sha256,
        approval_sha256=approval_sha256(approval),
        approval=approval,
        policies=entries,
    )
    lock = TrustedReviewerStoreLock(
        contract="evidence-domain-reviewer-trust-store-lock",
        schema_version="1.0",
        release_state="approved",
        trust_store_release=trust_store.trust_store_release,
        reviewer_trust_store_sha256=trust_sha256,
    )
    verified = verify_requirement_policy_release(
        release,
        trust_store=trust_store,
        trust_store_lock=lock,
        today=verification_date,
    )
    return ScenarioEvalPolicyFixture(
        verified=verified,
        trust_store=trust_store,
        trust_store_lock=lock,
    )


__all__ = [
    "SCENARIO_EVAL_POLICY_MODE",
    "SCENARIO_EVAL_POLICY_APPROVED_AT",
    "SCENARIO_EVAL_POLICY_RELEASE",
    "SCENARIO_EVAL_POLICY_VALID_FROM",
    "SCENARIO_EVAL_POLICY_VALID_UNTIL",
    "SCENARIO_EVAL_REQUIRED_DOCUMENT_CODE",
    "SCENARIO_EVAL_TRUST_RELEASE",
    "ScenarioEvalPolicyFixture",
    "build_scenario_eval_policy_fixture",
]
