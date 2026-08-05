from __future__ import annotations

import json
from pathlib import Path

from tools.check_contract_drift import (
    V3_READINESS_SCHEMA_PATH,
    V3_RETRIEVAL_SCHEMA_PATH,
    check_contract_drift,
)


def test_active_v3_schema_bytes_match_the_provider_models() -> None:
    assert check_contract_drift() == []


def test_active_contracts_are_provider_owned_v3_schemas() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    assert V3_RETRIEVAL_SCHEMA_PATH.is_relative_to(repository_root)
    assert V3_READINESS_SCHEMA_PATH.is_relative_to(repository_root)

    retrieval = json.loads(V3_RETRIEVAL_SCHEMA_PATH.read_text(encoding="utf-8"))
    readiness = json.loads(V3_READINESS_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert retrieval["$defs"]["RetrieveRequestV3"]["properties"]["schema_version"]["const"] == "3.0"
    assert readiness["properties"] == {
        "status": {"const": "ready", "title": "Status", "type": "string"},
    }
    assert "$defs" not in readiness
