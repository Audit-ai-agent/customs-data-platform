"""Verify the two provider-owned Evidence V3 schemas used by local profiles.

This intentionally checks only the live team boundary: V3 retrieval and
readiness. Historical release/trust/provenance schemas are not an active
contract and are deliberately absent from the check.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
LAW_REPOSITORY = ROOT / "law_repository"
V3_RETRIEVAL_SCHEMA_PATH = ROOT / "contracts" / "evidence" / "v3" / "rag_retrieval_v3.schema.json"
V3_READINESS_SCHEMA_PATH = ROOT / "contracts" / "evidence" / "v3" / "readiness_v3.schema.json"

sys.path.insert(0, str(LAW_REPOSITORY))

from src.evidence_v3 import RetrieveRequestV3, RetrieveResponseV3  # noqa: E402
from src.profile_wire import EvidenceRuntimeReadinessV3  # noqa: E402


def _without_document_metadata(schema: dict[str, Any]) -> dict[str, Any]:
    """Copy schema and remove the root metadata excluded from canonical bytes."""

    normalized = dict(schema)
    for key in ("$schema", "$id", "title"):
        normalized.pop(key, None)
    return normalized


def _diff(*, expected: dict[str, Any], actual: dict[str, Any], path: Path) -> str:
    expected_text = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True)
    actual_text = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True)
    return "\n".join(
        difflib.unified_diff(
            expected_text.splitlines(),
            actual_text.splitlines(),
            fromfile=f"canonical:{path.relative_to(ROOT)}",
            tofile="runtime-model",
            lineterm="",
        )
    )


def check_contract_drift() -> list[str]:
    """Return textual diffs if the active V3 models drift from canonical bytes."""

    generated = {
        V3_RETRIEVAL_SCHEMA_PATH: TypeAdapter(RetrieveRequestV3 | RetrieveResponseV3).json_schema(),
        V3_READINESS_SCHEMA_PATH: EvidenceRuntimeReadinessV3.model_json_schema(),
    }
    errors: list[str] = []
    for path, actual in generated.items():
        expected = _without_document_metadata(json.loads(path.read_text(encoding="utf-8")))
        normalized_actual = _without_document_metadata(actual)
        if expected != normalized_actual:
            errors.append(_diff(expected=expected, actual=normalized_actual, path=path))
    return errors


def main() -> int:
    errors = check_contract_drift()
    if errors:
        print("Evidence V3 contract drift detected:\n" + "\n\n".join(errors))
        return 1
    print(
        "Evidence V3 contracts match: "
        f"{V3_RETRIEVAL_SCHEMA_PATH.relative_to(ROOT)}, "
        f"{V3_READINESS_SCHEMA_PATH.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
