"""Generate the frozen provider-owned Evidence v3 JSON Schemas.

This command is intentionally explicit and is not run by the service.  CI
uses ``check_contract_drift.py`` to ensure the checked-in bytes still describe
the exact runtime models.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "law_repository"))

from src.evidence_v3 import RetrieveRequestV3, RetrieveResponseV3  # noqa: E402
from src.release_provenance_v3 import EvidenceV3ReleaseProvenance  # noqa: E402
from src.requirement_policy_release import (  # noqa: E402
    RequirementPolicyRelease,
    TrustedReviewerStore,
    TrustedReviewerStoreLock,
)
from src.runtime_release_v3 import EvidenceRuntimeReadinessV3  # noqa: E402


def _write(path: Path, schema: dict, *, schema_id: str, title: str) -> None:
    payload = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": schema_id,
        "title": title,
        **schema,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    output = ROOT / "contracts/evidence/v3"
    combined = TypeAdapter(RetrieveRequestV3 | RetrieveResponseV3).json_schema()
    _write(
        output / "rag_retrieval_v3.schema.json",
        combined,
        schema_id=(
            "https://customs-data-platform.invalid/contracts/evidence/v3/"
            "rag_retrieval_v3.schema.json"
        ),
        title="Customs Evidence Retrieval v3",
    )
    _write(
        output / "requirement_policy_v1.schema.json",
        RequirementPolicyRelease.model_json_schema(),
        schema_id=(
            "https://customs-data-platform.invalid/contracts/evidence/v3/"
            "requirement_policy_v1.schema.json"
        ),
        title="Requirement Verification Policy Release v1",
    )
    _write(
        output / "readiness_v3.schema.json",
        EvidenceRuntimeReadinessV3.model_json_schema(),
        schema_id=(
            "https://customs-data-platform.invalid/contracts/evidence/v3/readiness_v3.schema.json"
        ),
        title="Evidence Runtime Readiness v3",
    )
    _write(
        output / "trusted_reviewer_store_v1.schema.json",
        TrustedReviewerStore.model_json_schema(),
        schema_id=(
            "https://customs-data-platform.invalid/contracts/evidence/v3/"
            "trusted_reviewer_store_v1.schema.json"
        ),
        title="Trusted Domain Reviewer Store v1",
    )
    _write(
        output / "trusted_reviewer_lock_v1.schema.json",
        TrustedReviewerStoreLock.model_json_schema(),
        schema_id=(
            "https://customs-data-platform.invalid/contracts/evidence/v3/"
            "trusted_reviewer_lock_v1.schema.json"
        ),
        title="Trusted Domain Reviewer Store Lock v1",
    )
    _write(
        output / "release_provenance_v1.schema.json",
        EvidenceV3ReleaseProvenance.model_json_schema(),
        schema_id=(
            "https://customs-data-platform.invalid/contracts/evidence/v3/"
            "release_provenance_v1.schema.json"
        ),
        title="Evidence v3 Release Provenance v1",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
