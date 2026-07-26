"""Cryptographically approved supporting-document verification policy.

The policy file is not its own authority.  An ``approved`` release is usable
only when all of these independent boundaries agree:

* the policy and review claims form one domain-separated approval subject;
* two different allowlisted domain reviewers sign that exact subject with
  Ed25519 keys;
* the external reviewer trust store matches the immutable hash pinned in the
  provider image; and
* policy, trust-store, key, and approval validity windows are current.

The repository ships unreleased sentinels.  They can be parsed without an
external trust-store mount, but they can never produce a verified runtime
lease or readiness success.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.evidence_v3 import RequirementPolicyBinding, VerificationPolicy

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Ed25519PublicKeyHex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Ed25519SignatureHex = Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]
NonEmpty128 = Annotated[str, Field(min_length=1, max_length=128)]

HEADER_REQUIREMENT_POLICY_RELEASE = "X-Evidence-Requirement-Policy-Release"
HEADER_REQUIREMENT_POLICY_SHA256 = "X-Evidence-Requirement-Policy-SHA256"
HEADER_COVERAGE_MATRIX_SHA256 = "X-Evidence-Coverage-Matrix-SHA256"
HEADER_REVIEWER_TRUST_STORE_SHA256 = "X-Evidence-Reviewer-Trust-Store-SHA256"
HEADER_RUNTIME_V3_SHA256 = "X-Evidence-Runtime-V3-SHA256"

DEFAULT_POLICY_RELEASE_PATH = Path("/run/evidence-release/requirement-policy.json")
DEFAULT_REVIEWER_TRUST_STORE_PATH = Path("/run/evidence-release/trusted-reviewers.json")
DEFAULT_REVIEWER_TRUST_LOCK_PATH = Path("/app/contracts/evidence/v3/trusted-reviewers.lock.json")

MAX_POLICY_RELEASE_BYTES = 1_048_576
MAX_TRUST_STORE_BYTES = 1_048_576
MAX_TRUST_LOCK_BYTES = 16_384
MIN_INDEPENDENT_REVIEWERS = 2
APPROVAL_SIGNATURE_DOMAIN = b"customs-evidence/requirement-policy-approval/v1\x00"


class PolicyContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequirementPolicyEntry(PolicyContractModel):
    required_document_code: NonEmpty128
    verification_policy: VerificationPolicy
    policy_source_sha256: Sha256


class ReviewerAttestation(PolicyContractModel):
    reviewer_id: NonEmpty128
    key_id: NonEmpty128
    review_reference_sha256: Sha256
    signature_hex: Ed25519SignatureHex


class RequirementPolicyApproval(PolicyContractModel):
    reviewer_attestations: list[ReviewerAttestation] = Field(
        min_length=MIN_INDEPENDENT_REVIEWERS,
        max_length=10,
    )
    adjudication_sha256: Sha256
    subject_sha256: Sha256
    trust_store_release: NonEmpty128
    reviewer_trust_store_sha256: Sha256
    approved_at: date
    valid_until: date

    @model_validator(mode="after")
    def valid_approval(self) -> RequirementPolicyApproval:
        reviewer_ids = [row.reviewer_id for row in self.reviewer_attestations]
        key_ids = [row.key_id for row in self.reviewer_attestations]
        review_references = [row.review_reference_sha256 for row in self.reviewer_attestations]
        signatures = [row.signature_hex for row in self.reviewer_attestations]
        if len(reviewer_ids) != len(set(reviewer_ids)):
            raise ValueError("approval requires independent reviewer identities")
        if len(key_ids) != len(set(key_ids)):
            raise ValueError("approval reviewer keys must be unique")
        if len(review_references) != len(set(review_references)):
            raise ValueError("approval requires independent review references")
        if len(signatures) != len(set(signatures)):
            raise ValueError("approval reviewer signatures must be unique")
        if self.valid_until < self.approved_at:
            raise ValueError("approval validity window is invalid")
        return self


class RequirementPolicyRelease(PolicyContractModel):
    contract: Literal["requirement-verification-policy"]
    schema_version: Literal["1.0"]
    release_state: Literal["unreleased", "approved"]
    policy_release: NonEmpty128
    knowledge_source_manifest_sha256: Sha256 | None
    knowledge_snapshot_id: str | None = Field(default=None, min_length=1, max_length=200)
    coverage_matrix_sha256: Sha256 | None
    policy_manifest_sha256: Sha256 | None
    approval_sha256: Sha256 | None
    approval: RequirementPolicyApproval | None
    policies: list[RequirementPolicyEntry] = Field(max_length=10_000)

    @model_validator(mode="after")
    def release_is_fail_closed(self) -> RequirementPolicyRelease:
        codes = [entry.required_document_code for entry in self.policies]
        if len(codes) != len(set(codes)):
            raise ValueError("requirement policy document codes must be unique")
        if self.release_state == "unreleased":
            if (
                any(
                    value is not None
                    for value in (
                        self.coverage_matrix_sha256,
                        self.knowledge_source_manifest_sha256,
                        self.knowledge_snapshot_id,
                        self.policy_manifest_sha256,
                        self.approval_sha256,
                        self.approval,
                    )
                )
                or self.policies
            ):
                raise ValueError("unreleased policy cannot contain approval claims")
            return self
        if (
            self.coverage_matrix_sha256 is None
            or self.knowledge_source_manifest_sha256 is None
            or self.knowledge_snapshot_id is None
            or self.policy_manifest_sha256 is None
            or self.approval_sha256 is None
            or self.approval is None
            or not self.policies
        ):
            raise ValueError("approved policy release requires complete approval evidence")
        if self.approval.subject_sha256 != approval_subject_sha256(self):
            raise ValueError("requirement policy approval subject mismatch")
        return self

    def assert_current_approved(self, *, today: date | None = None) -> None:
        if self.release_state != "approved" or self.approval is None:
            raise ValueError("requirement verification policy is not approved")
        effective_today = today or date.today()
        if self.approval.approved_at > effective_today:
            raise ValueError("requirement verification policy approval is not active yet")
        if self.approval.valid_until < effective_today:
            raise ValueError("requirement verification policy approval is expired")


class TrustedReviewerKey(PolicyContractModel):
    reviewer_id: NonEmpty128
    key_id: NonEmpty128
    role: Literal["customs_domain_reviewer"]
    algorithm: Literal["ed25519"]
    public_key_hex: Ed25519PublicKeyHex
    valid_from: date
    valid_until: date
    revoked: bool = False

    @model_validator(mode="after")
    def validity_is_ordered(self) -> TrustedReviewerKey:
        if self.valid_until < self.valid_from:
            raise ValueError("trusted reviewer key validity window is invalid")
        return self


class TrustedReviewerStore(PolicyContractModel):
    contract: Literal["evidence-domain-reviewer-trust-store"]
    schema_version: Literal["1.0"]
    trust_store_release: NonEmpty128
    valid_from: date
    valid_until: date
    keys: list[TrustedReviewerKey] = Field(min_length=MIN_INDEPENDENT_REVIEWERS, max_length=100)

    @model_validator(mode="after")
    def closed_unique_keys(self) -> TrustedReviewerStore:
        if self.valid_until < self.valid_from:
            raise ValueError("trusted reviewer store validity window is invalid")
        key_ids = [row.key_id for row in self.keys]
        if len(key_ids) != len(set(key_ids)):
            raise ValueError("trusted reviewer key IDs must be unique")
        public_keys = [row.public_key_hex for row in self.keys]
        if len(public_keys) != len(set(public_keys)):
            raise ValueError(
                "trusted reviewer public keys must be globally unique; "
                "revoked or historical key material cannot be re-aliased"
            )
        non_revoked_keys = [row for row in self.keys if not row.revoked]
        active_reviewer_ids = {row.reviewer_id for row in non_revoked_keys}
        if len(active_reviewer_ids) < MIN_INDEPENDENT_REVIEWERS:
            raise ValueError("trusted reviewer store requires two independent reviewers")
        return self

    def assert_current(self, *, today: date | None = None) -> None:
        effective_today = today or date.today()
        if self.valid_from > effective_today:
            raise ValueError("trusted reviewer store is not active yet")
        if self.valid_until < effective_today:
            raise ValueError("trusted reviewer store is expired")


class TrustedReviewerStoreLock(PolicyContractModel):
    contract: Literal["evidence-domain-reviewer-trust-store-lock"]
    schema_version: Literal["1.0"]
    release_state: Literal["unreleased", "approved"]
    trust_store_release: NonEmpty128
    reviewer_trust_store_sha256: Sha256 | None

    @model_validator(mode="after")
    def lock_is_fail_closed(self) -> TrustedReviewerStoreLock:
        if self.release_state == "unreleased":
            if self.reviewer_trust_store_sha256 is not None:
                raise ValueError("unreleased trust-store lock cannot pin a reviewer store")
            return self
        if self.reviewer_trust_store_sha256 is None:
            raise ValueError("approved trust-store lock requires an exact store hash")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedRequirementPolicyRelease:
    """A policy proven against the immutable reviewer trust root."""

    release: RequirementPolicyRelease
    reviewer_trust_store_sha256: str

    @property
    def verified_approval_sha256(self) -> str:
        if self.release.approval_sha256 is None:
            raise ValueError("verified policy approval digest is unavailable")
        return _canonical_sha256(
            {
                "domain": "customs-evidence/verified-policy-approval/v1",
                "policy_approval_sha256": self.release.approval_sha256,
                "reviewer_trust_store_sha256": self.reviewer_trust_store_sha256,
            }
        )

    def bindings(self) -> dict[str, RequirementPolicyBinding]:
        self.release.assert_current_approved()
        return {
            entry.required_document_code: RequirementPolicyBinding(
                required_document_code=entry.required_document_code,
                verification_policy=entry.verification_policy,
                policy_source_sha256=entry.policy_source_sha256,
                policy_release=self.release.policy_release,
                approved=True,
            )
            for entry in self.release.policies
        }


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def approval_sha256(approval: RequirementPolicyApproval) -> str:
    return _canonical_sha256(approval.model_dump(mode="json"))


def policy_manifest_sha256(entries: list[RequirementPolicyEntry]) -> str:
    return _canonical_sha256([entry.model_dump(mode="json") for entry in entries])


def trusted_reviewer_store_sha256(store: TrustedReviewerStore) -> str:
    return _canonical_sha256(store.model_dump(mode="json"))


def policy_approval_subject_sha256(
    *,
    policy_release: str,
    knowledge_source_manifest_sha256: str | None,
    knowledge_snapshot_id: str | None,
    coverage_matrix_sha256: str | None,
    policy_manifest_sha256: str | None,
    adjudication_sha256: str,
    reviewer_claims: list[dict[str, str]],
    trust_store_release: str,
    reviewer_trust_store_sha256: str,
    approved_at: date,
    valid_until: date,
) -> str:
    """Hash the exact common subject signed by every independent reviewer."""

    claims = sorted(
        reviewer_claims,
        key=lambda row: (row["reviewer_id"], row["key_id"]),
    )
    return _canonical_sha256(
        {
            "domain": "customs-evidence/requirement-policy-approval/v1",
            "contract": "requirement-verification-policy",
            "schema_version": "1.0",
            "policy_release": policy_release,
            "knowledge_source_manifest_sha256": knowledge_source_manifest_sha256,
            "knowledge_snapshot_id": knowledge_snapshot_id,
            "coverage_matrix_sha256": coverage_matrix_sha256,
            "policy_manifest_sha256": policy_manifest_sha256,
            "adjudication_sha256": adjudication_sha256,
            "reviewer_claims": claims,
            "trust_store_release": trust_store_release,
            "reviewer_trust_store_sha256": reviewer_trust_store_sha256,
            "approved_at": approved_at.isoformat(),
            "valid_until": valid_until.isoformat(),
        }
    )


def approval_subject_sha256(release: RequirementPolicyRelease) -> str:
    if release.approval is None:
        raise ValueError("approval subject requires an approval")
    approval = release.approval
    return policy_approval_subject_sha256(
        policy_release=release.policy_release,
        knowledge_source_manifest_sha256=release.knowledge_source_manifest_sha256,
        knowledge_snapshot_id=release.knowledge_snapshot_id,
        coverage_matrix_sha256=release.coverage_matrix_sha256,
        policy_manifest_sha256=release.policy_manifest_sha256,
        adjudication_sha256=approval.adjudication_sha256,
        reviewer_claims=[
            {
                "reviewer_id": row.reviewer_id,
                "key_id": row.key_id,
                "review_reference_sha256": row.review_reference_sha256,
            }
            for row in approval.reviewer_attestations
        ],
        trust_store_release=approval.trust_store_release,
        reviewer_trust_store_sha256=approval.reviewer_trust_store_sha256,
        approved_at=approval.approved_at,
        valid_until=approval.valid_until,
    )


def approval_signature_message(subject_sha256: str) -> bytes:
    if len(subject_sha256) != 64 or any(char not in "0123456789abcdef" for char in subject_sha256):
        raise ValueError("approval subject SHA-256 is invalid")
    return APPROVAL_SIGNATURE_DOMAIN + bytes.fromhex(subject_sha256)


def _strict_json_model_bytes(
    raw: bytes,
    model_type: type[PolicyContractModel],
    maximum: int,
):
    try:
        if len(raw) < 1 or len(raw) > maximum:
            raise ValueError("release artifact has an invalid size")

        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("release artifact contains duplicate key")
                result[key] = value
            return result

        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("release artifact contains non-finite value")
            ),
        )
        if not isinstance(payload, dict):
            raise ValueError("release artifact must be a JSON object")
        return model_type.model_validate(payload)
    except Exception as exc:
        raise ValueError("release artifact is unreadable") from exc


def _strict_json_model(path: Path, model_type: type[PolicyContractModel], maximum: int):
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("release artifact must be a regular non-symlink file")
        size = path.stat().st_size
        if size < 1 or size > maximum:
            raise ValueError("release artifact has an invalid size")
        raw = path.read_bytes()
        if len(raw) != size:
            raise ValueError("release artifact changed during read")
    except Exception as exc:
        raise ValueError("release artifact is unreadable") from exc
    return _strict_json_model_bytes(raw, model_type, maximum)


def parse_requirement_policy_release(raw: bytes) -> RequirementPolicyRelease:
    """Parse one already-attested byte string without reopening its path."""

    release = _strict_json_model_bytes(
        raw,
        RequirementPolicyRelease,
        MAX_POLICY_RELEASE_BYTES,
    )
    if not isinstance(release, RequirementPolicyRelease):
        raise ValueError("requirement policy release type mismatch")
    if release.release_state == "approved":
        if (
            release.approval is None
            or release.policy_manifest_sha256 is None
            or release.approval_sha256 is None
        ):
            raise ValueError("approved policy release is incomplete")
        if policy_manifest_sha256(release.policies) != release.policy_manifest_sha256:
            raise ValueError("requirement policy manifest hash mismatch")
        if approval_sha256(release.approval) != release.approval_sha256:
            raise ValueError("requirement policy approval hash mismatch")
        release.assert_current_approved()
    return release


def parse_trusted_reviewer_store(raw: bytes) -> TrustedReviewerStore:
    store = _strict_json_model_bytes(raw, TrustedReviewerStore, MAX_TRUST_STORE_BYTES)
    if not isinstance(store, TrustedReviewerStore):
        raise ValueError("trusted reviewer store type mismatch")
    store.assert_current()
    return store


def parse_trusted_reviewer_store_lock(raw: bytes) -> TrustedReviewerStoreLock:
    lock = _strict_json_model_bytes(raw, TrustedReviewerStoreLock, MAX_TRUST_LOCK_BYTES)
    if not isinstance(lock, TrustedReviewerStoreLock):
        raise ValueError("trusted reviewer store lock type mismatch")
    return lock


def load_requirement_policy_release(path: Path) -> RequirementPolicyRelease:
    """Parse and internally validate a policy; this does not grant approval."""

    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("release artifact must be a regular non-symlink file")
        size = path.stat().st_size
        if size < 1 or size > MAX_POLICY_RELEASE_BYTES:
            raise ValueError("release artifact has an invalid size")
        raw = path.read_bytes()
        if len(raw) != size:
            raise ValueError("release artifact changed during read")
    except Exception as exc:
        raise ValueError("release artifact is unreadable") from exc
    return parse_requirement_policy_release(raw)


def load_trusted_reviewer_store(path: Path) -> TrustedReviewerStore:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("release artifact must be a regular non-symlink file")
        size = path.stat().st_size
        if size < 1 or size > MAX_TRUST_STORE_BYTES:
            raise ValueError("release artifact has an invalid size")
        raw = path.read_bytes()
        if len(raw) != size:
            raise ValueError("release artifact changed during read")
    except Exception as exc:
        raise ValueError("release artifact is unreadable") from exc
    return parse_trusted_reviewer_store(raw)


def load_trusted_reviewer_store_lock(path: Path) -> TrustedReviewerStoreLock:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("release artifact must be a regular non-symlink file")
        size = path.stat().st_size
        if size < 1 or size > MAX_TRUST_LOCK_BYTES:
            raise ValueError("release artifact has an invalid size")
        raw = path.read_bytes()
        if len(raw) != size:
            raise ValueError("release artifact changed during read")
    except Exception as exc:
        raise ValueError("release artifact is unreadable") from exc
    return parse_trusted_reviewer_store_lock(raw)


def verify_requirement_policy_release(
    release: RequirementPolicyRelease,
    *,
    trust_store: TrustedReviewerStore,
    trust_store_lock: TrustedReviewerStoreLock,
    today: date | None = None,
) -> VerifiedRequirementPolicyRelease:
    """Verify two independent signatures against the image-pinned trust root."""

    release.assert_current_approved(today=today)
    if (
        release.approval is None
        or release.policy_manifest_sha256 is None
        or release.approval_sha256 is None
        or policy_manifest_sha256(release.policies) != release.policy_manifest_sha256
        or approval_sha256(release.approval) != release.approval_sha256
    ):
        raise ValueError("requirement policy approval integrity check failed")
    trust_store.assert_current(today=today)
    if trust_store_lock.release_state != "approved":
        raise ValueError("trusted reviewer store lock is not approved")
    actual_store_sha256 = trusted_reviewer_store_sha256(trust_store)
    if (
        trust_store_lock.reviewer_trust_store_sha256 != actual_store_sha256
        or trust_store_lock.trust_store_release != trust_store.trust_store_release
    ):
        raise ValueError("trusted reviewer store does not match immutable provider lock")
    approval = release.approval
    if (
        approval.trust_store_release != trust_store.trust_store_release
        or approval.reviewer_trust_store_sha256 != actual_store_sha256
    ):
        raise ValueError("requirement policy approval uses another reviewer trust store")
    if approval.subject_sha256 != approval_subject_sha256(release):
        raise ValueError("requirement policy approval subject mismatch")

    keys = {key.key_id: key for key in trust_store.keys}
    effective_today = today or date.today()
    verified_reviewers: set[str] = set()
    verified_key_ids: set[str] = set()
    verified_public_keys: set[str] = set()
    verified_signatures: set[str] = set()
    message = approval_signature_message(approval.subject_sha256)
    for attestation in approval.reviewer_attestations:
        key = keys.get(attestation.key_id)
        if key is None or key.reviewer_id != attestation.reviewer_id:
            raise ValueError("requirement policy approval uses an unknown reviewer key")
        if key.revoked:
            raise ValueError("requirement policy approval uses a revoked reviewer key")
        if not (
            key.valid_from <= approval.approved_at <= key.valid_until
            and key.valid_from <= effective_today <= key.valid_until
        ):
            raise ValueError("requirement policy approval uses an inactive reviewer key")
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(key.public_key_hex)).verify(
                bytes.fromhex(attestation.signature_hex),
                message,
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("requirement policy reviewer signature is invalid") from exc
        if key.key_id in verified_key_ids:
            raise ValueError("requirement policy approval reuses a reviewer key")
        if key.public_key_hex in verified_public_keys:
            raise ValueError("requirement policy approval reuses reviewer public-key material")
        if attestation.signature_hex in verified_signatures:
            raise ValueError("requirement policy approval reuses a reviewer signature")
        verified_reviewers.add(attestation.reviewer_id)
        verified_key_ids.add(key.key_id)
        verified_public_keys.add(key.public_key_hex)
        verified_signatures.add(attestation.signature_hex)
    if (
        min(
            len(verified_reviewers),
            len(verified_key_ids),
            len(verified_public_keys),
            len(verified_signatures),
        )
        < MIN_INDEPENDENT_REVIEWERS
    ):
        raise ValueError("requirement policy approval requires two independent reviewers")
    return VerifiedRequirementPolicyRelease(
        release=release,
        reviewer_trust_store_sha256=actual_store_sha256,
    )


def load_verified_requirement_policy_release(
    policy_path: Path = DEFAULT_POLICY_RELEASE_PATH,
    trust_store_path: Path = DEFAULT_REVIEWER_TRUST_STORE_PATH,
    trust_lock_path: Path = DEFAULT_REVIEWER_TRUST_LOCK_PATH,
) -> VerifiedRequirementPolicyRelease:
    release = load_requirement_policy_release(policy_path)
    # Deliberately fail before touching the external trust-store mount.  This
    # keeps the checked-in unreleased sentinel parseable while never ready.
    release.assert_current_approved()
    trust_store_lock = load_trusted_reviewer_store_lock(trust_lock_path)
    trust_store = load_trusted_reviewer_store(trust_store_path)
    return verify_requirement_policy_release(
        release,
        trust_store=trust_store,
        trust_store_lock=trust_store_lock,
    )


__all__ = [
    "APPROVAL_SIGNATURE_DOMAIN",
    "DEFAULT_POLICY_RELEASE_PATH",
    "DEFAULT_REVIEWER_TRUST_LOCK_PATH",
    "DEFAULT_REVIEWER_TRUST_STORE_PATH",
    "HEADER_COVERAGE_MATRIX_SHA256",
    "HEADER_REQUIREMENT_POLICY_RELEASE",
    "HEADER_REQUIREMENT_POLICY_SHA256",
    "HEADER_REVIEWER_TRUST_STORE_SHA256",
    "HEADER_RUNTIME_V3_SHA256",
    "MAX_POLICY_RELEASE_BYTES",
    "MIN_INDEPENDENT_REVIEWERS",
    "RequirementPolicyApproval",
    "RequirementPolicyEntry",
    "RequirementPolicyRelease",
    "ReviewerAttestation",
    "TrustedReviewerKey",
    "TrustedReviewerStore",
    "TrustedReviewerStoreLock",
    "VerifiedRequirementPolicyRelease",
    "approval_sha256",
    "approval_signature_message",
    "approval_subject_sha256",
    "load_requirement_policy_release",
    "load_trusted_reviewer_store",
    "load_trusted_reviewer_store_lock",
    "load_verified_requirement_policy_release",
    "parse_requirement_policy_release",
    "parse_trusted_reviewer_store",
    "parse_trusted_reviewer_store_lock",
    "policy_manifest_sha256",
    "policy_approval_subject_sha256",
    "trusted_reviewer_store_sha256",
    "verify_requirement_policy_release",
]
