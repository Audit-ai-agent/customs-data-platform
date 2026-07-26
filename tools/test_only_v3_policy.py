"""Synthetic Evidence V3 approval material for tests and loopback conformance.

This module is deliberately outside ``law_repository/src`` and outside the
production image allowlist.  Its deterministic private keys are public test
fixtures and MUST NOT be used for a release, deployment, RAG evaluation, or
automatic-completion decision.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta

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

_TEST_ONLY_PRIVATE_KEY_DOMAIN = (
    b"customs-data-platform/test-only-v3-conformance/private-key/NON-PRODUCTION\x00"
)
_TEST_ONLY_RELEASE = "test-only-conformance.no-production-policy.v1"
_TEST_ONLY_TRUST_RELEASE = "test-only-conformance.no-production-trust.v1"


@dataclass(frozen=True, slots=True)
class TestOnlyPolicyFixture:
    verified: VerifiedRequirementPolicyRelease
    trust_store: TrustedReviewerStore
    trust_store_lock: TrustedReviewerStoreLock


def _private_key(index: int) -> Ed25519PrivateKey:
    seed = hashlib.sha256(_TEST_ONLY_PRIVATE_KEY_DOMAIN + str(index).encode("ascii")).digest()
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


def build_test_only_policy_fixture(
    *,
    knowledge_source_manifest_sha256: str,
    knowledge_snapshot_id: str,
    required_document_code: str = "TEST_ONLY_IMPORT_CONFIRMATION",
    today: date | None = None,
) -> TestOnlyPolicyFixture:
    """Build two valid signatures while preserving unmistakable non-release identity."""

    effective_today = today or date.today()
    valid_from = effective_today - timedelta(days=1)
    valid_until = effective_today + timedelta(days=30)
    private_keys = [_private_key(1), _private_key(2)]
    reviewer_claims = [
        {
            "reviewer_id": f"test-only-reviewer-{index}",
            "key_id": f"test-only-ed25519-key-{index}",
            "review_reference_sha256": hashlib.sha256(
                f"test-only-review-reference-{index}".encode("ascii")
            ).hexdigest(),
        }
        for index in (1, 2)
    ]
    trust_store = TrustedReviewerStore(
        contract="evidence-domain-reviewer-trust-store",
        schema_version="1.0",
        trust_store_release=_TEST_ONLY_TRUST_RELEASE,
        valid_from=valid_from,
        valid_until=valid_until,
        keys=[
            TrustedReviewerKey(
                reviewer_id=claim["reviewer_id"],
                key_id=claim["key_id"],
                role="customs_domain_reviewer",
                algorithm="ed25519",
                public_key_hex=_public_key_hex(private_key),
                valid_from=valid_from,
                valid_until=valid_until,
                revoked=False,
            )
            for claim, private_key in zip(reviewer_claims, private_keys, strict=True)
        ],
    )
    trust_sha256 = trusted_reviewer_store_sha256(trust_store)
    entries = [
        RequirementPolicyEntry(
            required_document_code=required_document_code,
            verification_policy="content_match_required",
            policy_source_sha256=hashlib.sha256(
                b"test-only-policy-source/NON-PRODUCTION"
            ).hexdigest(),
        )
    ]
    manifest_sha256 = policy_manifest_sha256(entries)
    adjudication_sha256 = hashlib.sha256(b"test-only-adjudication/NON-PRODUCTION").hexdigest()
    coverage_sha256 = hashlib.sha256(b"test-only-coverage-matrix/NON-PRODUCTION").hexdigest()
    subject_sha256 = policy_approval_subject_sha256(
        policy_release=_TEST_ONLY_RELEASE,
        knowledge_source_manifest_sha256=knowledge_source_manifest_sha256,
        knowledge_snapshot_id=knowledge_snapshot_id,
        coverage_matrix_sha256=coverage_sha256,
        policy_manifest_sha256=manifest_sha256,
        adjudication_sha256=adjudication_sha256,
        reviewer_claims=reviewer_claims,
        trust_store_release=trust_store.trust_store_release,
        reviewer_trust_store_sha256=trust_sha256,
        approved_at=effective_today,
        valid_until=valid_until,
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
        approved_at=effective_today,
        valid_until=valid_until,
    )
    release = RequirementPolicyRelease(
        contract="requirement-verification-policy",
        schema_version="1.0",
        release_state="approved",
        policy_release=_TEST_ONLY_RELEASE,
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
        today=effective_today,
    )
    return TestOnlyPolicyFixture(
        verified=verified,
        trust_store=trust_store,
        trust_store_lock=lock,
    )


__all__ = ["TestOnlyPolicyFixture", "build_test_only_policy_fixture"]
