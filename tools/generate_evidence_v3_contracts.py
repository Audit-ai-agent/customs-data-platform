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
from src.guidance_v3 import (  # noqa: E402
    GeneralGuidanceQueryRequestV3,
    GeneralGuidanceQueryResponseV3,
)
from src.guidance_registry import registry_manifest  # noqa: E402
from src.guidance_registry_v2 import registry_manifest_v2  # noqa: E402
from src.profile_wire import EvidenceRuntimeReadinessV3  # noqa: E402


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


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
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
        output / "readiness_v3.schema.json",
        EvidenceRuntimeReadinessV3.model_json_schema(),
        schema_id=(
            "https://customs-data-platform.invalid/contracts/evidence/v3/readiness_v3.schema.json"
        ),
        title="Evidence Runtime Readiness v3",
    )
    _write(
        output / "guidance_query_v3.schema.json",
        TypeAdapter(GeneralGuidanceQueryRequestV3 | GeneralGuidanceQueryResponseV3).json_schema(),
        schema_id=(
            "https://customs-data-platform.invalid/contracts/evidence/v3/"
            "guidance_query_v3.schema.json"
        ),
        title="Customs General Guidance Query v3",
    )
    _write_manifest(output / "guidance_registry_v1.manifest.json", registry_manifest())
    _write_manifest(output / "guidance_registry_v2.manifest.json", registry_manifest_v2())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
