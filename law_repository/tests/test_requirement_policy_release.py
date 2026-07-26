from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.requirement_policy_release import (
    MAX_POLICY_RELEASE_BYTES,
    MAX_TRUST_LOCK_BYTES,
    MAX_TRUST_STORE_BYTES,
    RequirementPolicyRelease,
    TrustedReviewerStore,
    TrustedReviewerStoreLock,
    approval_sha256,
    approval_subject_sha256,
    load_requirement_policy_release,
    load_trusted_reviewer_store,
    load_trusted_reviewer_store_lock,
    load_verified_requirement_policy_release,
    verify_requirement_policy_release,
)
from tools.test_only_v3_policy import build_test_only_policy_fixture

ROOT = Path(__file__).resolve().parents[2]
POLICY_SENTINEL = ROOT / "contracts/evidence/v3/requirement-policy.lock.json"
TRUST_SENTINEL = ROOT / "contracts/evidence/v3/trusted-reviewers.lock.json"


def _fixture():
    return build_test_only_policy_fixture(
        knowledge_source_manifest_sha256="6" * 64,
        knowledge_snapshot_id="test-only-snapshot",
        required_document_code="DOC-A",
    )


def _write_json(path: Path, value: object) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    path.write_text(json.dumps(value), encoding="utf-8")


def test_packaged_policy_and_trust_locks_are_intentionally_unreleased() -> None:
    release = load_requirement_policy_release(POLICY_SENTINEL)
    lock = json.loads(TRUST_SENTINEL.read_text(encoding="utf-8"))
    assert release.release_state == "unreleased"
    assert lock["release_state"] == "unreleased"
    with pytest.raises(ValueError, match="not approved"):
        release.assert_current_approved()


def test_unreleased_policy_does_not_require_external_trust_store(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not approved"):
        load_verified_requirement_policy_release(
            POLICY_SENTINEL,
            tmp_path / "missing-trust.json",
            TRUST_SENTINEL,
        )


def test_two_independent_ed25519_signatures_verify_against_pinned_store() -> None:
    fixture = _fixture()
    assert fixture.verified.release.policy_release.startswith("test-only-conformance")
    assert fixture.verified.bindings()["DOC-A"].approved is True


def test_approved_policy_cannot_self_assert_without_trust_root(tmp_path: Path) -> None:
    fixture = _fixture()
    policy_path = tmp_path / "policy.json"
    lock_path = tmp_path / "trust-lock.json"
    _write_json(policy_path, fixture.verified.release)
    _write_json(lock_path, fixture.trust_store_lock)

    with pytest.raises(ValueError, match="unreadable"):
        load_verified_requirement_policy_release(
            policy_path,
            tmp_path / "missing-trust.json",
            lock_path,
        )


def test_loader_recomputes_policy_and_approval_hashes(tmp_path: Path) -> None:
    fixture = _fixture()
    payload = fixture.verified.release.model_dump(mode="json")
    path = tmp_path / "release.json"
    _write_json(path, payload)
    assert load_requirement_policy_release(path).policy_release.startswith("test-only")

    payload["policies"][0]["policy_source_sha256"] = "0" * 64
    _write_json(path, payload)
    with pytest.raises(ValueError, match="manifest hash mismatch"):
        load_requirement_policy_release(path)


def test_policy_is_bound_to_reviewed_subject() -> None:
    payload = _fixture().verified.release.model_dump(mode="json")
    payload["knowledge_snapshot_id"] = "different-snapshot"
    with pytest.raises(ValueError, match="approval subject mismatch"):
        RequirementPolicyRelease.model_validate(payload)


def test_approval_requires_different_reviewer_and_key_ids() -> None:
    payload = _fixture().verified.release.model_dump(mode="json")
    attestations = payload["approval"]["reviewer_attestations"]
    attestations[1]["reviewer_id"] = attestations[0]["reviewer_id"]
    with pytest.raises(ValueError, match="independent reviewer"):
        RequirementPolicyRelease.model_validate(payload)


def test_approval_rejects_reused_review_reference_and_signature() -> None:
    payload = _fixture().verified.release.model_dump(mode="json")
    attestations = payload["approval"]["reviewer_attestations"]
    attestations[1]["review_reference_sha256"] = attestations[0]["review_reference_sha256"]
    with pytest.raises(ValueError, match="independent review references"):
        RequirementPolicyRelease.model_validate(payload)

    payload = _fixture().verified.release.model_dump(mode="json")
    attestations = payload["approval"]["reviewer_attestations"]
    attestations[1]["signature_hex"] = attestations[0]["signature_hex"]
    with pytest.raises(ValueError, match="signatures must be unique"):
        RequirementPolicyRelease.model_validate(payload)


def test_approval_is_not_current_before_approval_date() -> None:
    release = _fixture().verified.release
    assert release.approval is not None
    with pytest.raises(ValueError, match="not active yet"):
        release.assert_current_approved(today=release.approval.approved_at - timedelta(days=1))


def test_unreleased_policy_cannot_self_assert_entries() -> None:
    payload = json.loads(POLICY_SENTINEL.read_text(encoding="utf-8"))
    payload["policies"] = [
        {
            "required_document_code": "DOC-A",
            "verification_policy": "presence_only",
            "policy_source_sha256": "1" * 64,
        }
    ]
    with pytest.raises(ValueError, match="unreleased policy"):
        RequirementPolicyRelease.model_validate(payload)


def test_tampered_trust_store_cannot_match_immutable_lock() -> None:
    fixture = _fixture()
    payload = fixture.trust_store.model_dump(mode="json")
    payload["keys"][0]["public_key_hex"] = "0" * 64
    tampered = TrustedReviewerStore.model_validate(payload)
    with pytest.raises(ValueError, match="immutable provider lock"):
        verify_requirement_policy_release(
            fixture.verified.release,
            trust_store=tampered,
            trust_store_lock=fixture.trust_store_lock,
        )


def test_unreleased_trust_lock_cannot_authorize_valid_signatures() -> None:
    fixture = _fixture()
    lock = TrustedReviewerStoreLock.model_validate(
        json.loads(TRUST_SENTINEL.read_text(encoding="utf-8"))
    )
    with pytest.raises(ValueError, match="lock is not approved"):
        verify_requirement_policy_release(
            fixture.verified.release,
            trust_store=fixture.trust_store,
            trust_store_lock=lock,
        )


def test_unknown_reviewer_key_is_rejected_before_signature_use() -> None:
    fixture = _fixture()
    release = fixture.verified.release
    assert release.approval is not None
    changed_rows = [
        release.approval.reviewer_attestations[0].model_copy(update={"key_id": "unknown-key"}),
        release.approval.reviewer_attestations[1],
    ]
    draft_approval = release.approval.model_copy(update={"reviewer_attestations": changed_rows})
    draft_release = release.model_copy(update={"approval": draft_approval})
    changed_approval = draft_approval.model_copy(
        update={"subject_sha256": approval_subject_sha256(draft_release)}
    )
    forged = release.model_copy(
        update={
            "approval": changed_approval,
            "approval_sha256": approval_sha256(changed_approval),
        }
    )
    with pytest.raises(ValueError, match="unknown reviewer key"):
        verify_requirement_policy_release(
            forged,
            trust_store=fixture.trust_store,
            trust_store_lock=fixture.trust_store_lock,
        )


def test_invalid_reviewer_signature_is_rejected() -> None:
    fixture = _fixture()
    release = fixture.verified.release
    assert release.approval is not None
    approval = release.approval.model_copy(
        update={
            "reviewer_attestations": [
                release.approval.reviewer_attestations[0].model_copy(
                    update={"signature_hex": "0" * 128}
                ),
                release.approval.reviewer_attestations[1],
            ]
        }
    )
    forged = release.model_copy(
        update={"approval": approval, "approval_sha256": approval_sha256(approval)}
    )
    with pytest.raises(ValueError, match="signature is invalid"):
        verify_requirement_policy_release(
            forged,
            trust_store=fixture.trust_store,
            trust_store_lock=fixture.trust_store_lock,
        )


def test_expired_trust_store_is_rejected() -> None:
    fixture = _fixture()
    expired = fixture.trust_store.model_copy(
        update={
            "valid_from": date.today() - timedelta(days=10),
            "valid_until": date.today() - timedelta(days=1),
        }
    )
    with pytest.raises(ValueError, match="expired"):
        verify_requirement_policy_release(
            fixture.verified.release,
            trust_store=expired,
            trust_store_lock=fixture.trust_store_lock,
        )


def test_revoked_key_cannot_leave_an_apparently_valid_two_reviewer_store() -> None:
    fixture = _fixture()
    payload = fixture.trust_store.model_dump(mode="json")
    payload["keys"][0]["revoked"] = True
    with pytest.raises(ValueError, match="two independent reviewers"):
        TrustedReviewerStore.model_validate(payload)


def test_public_key_cannot_be_aliased_to_a_second_reviewer_or_historical_row() -> None:
    fixture = _fixture()
    payload = fixture.trust_store.model_dump(mode="json")
    payload["keys"][1]["public_key_hex"] = payload["keys"][0]["public_key_hex"]
    with pytest.raises(ValueError, match="public keys must be globally unique"):
        TrustedReviewerStore.model_validate(payload)

    payload = fixture.trust_store.model_dump(mode="json")
    historical_alias = dict(payload["keys"][0])
    historical_alias["key_id"] = "revoked-historical-alias"
    historical_alias["revoked"] = True
    payload["keys"].append(historical_alias)
    with pytest.raises(ValueError, match="revoked or historical key material"):
        TrustedReviewerStore.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"contract":"requirement-verification-policy","contract":"duplicate"}',
        b'{"contract":NaN}',
    ],
)
def test_loader_rejects_noncanonical_json(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "policy.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError, match="unreadable"):
        load_requirement_policy_release(path)


def test_loader_rejects_oversized_policy_before_parse(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_POLICY_RELEASE_BYTES + 1))
    with pytest.raises(ValueError, match="unreadable"):
        load_requirement_policy_release(oversized)


@pytest.mark.parametrize(
    ("name", "maximum", "loader"),
    [
        ("policy.json", MAX_POLICY_RELEASE_BYTES, load_requirement_policy_release),
        ("reviewers.json", MAX_TRUST_STORE_BYTES, load_trusted_reviewer_store),
        ("reviewers-lock.json", MAX_TRUST_LOCK_BYTES, load_trusted_reviewer_store_lock),
    ],
)
def test_loaders_reject_oversized_files_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    maximum: int,
    loader: Callable[[Path], object],
) -> None:
    oversized = tmp_path / name
    oversized.write_bytes(b" " * (maximum + 1))
    original = Path.read_bytes

    def guarded_read(path: Path) -> bytes:
        if path == oversized:
            pytest.fail("oversized release artifact was read before its bound was checked")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    with pytest.raises(ValueError, match="unreadable"):
        loader(oversized)


def test_loader_rejects_symlink_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = tmp_path / "policy-link.json"
    link.write_text("{}", encoding="utf-8")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == link or original(self))
    with pytest.raises(ValueError, match="unreadable"):
        load_requirement_policy_release(link)
