from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.check_contract_drift import SCHEMA_PATH, check_contract_drift


EXPECTED_V1_SHA256 = "531b59ba4e86f78cef20a4fca052996efd8b88c48853ac12113ca76e69cdd69a"


def test_provider_owned_schema_is_the_frozen_v1_artifact():
    payload = SCHEMA_PATH.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == EXPECTED_V1_SHA256
    schema = json.loads(payload)
    assert schema["$defs"]["RetrieveRequest"]["properties"]["schema_version"] == {
        "const": "1.0"
    }


def test_fastapi_openapi_has_no_evidence_v1_contract_drift():
    assert check_contract_drift() == []


def test_schema_path_is_provider_owned_not_a_sibling_repository():
    repo_root = Path(__file__).resolve().parents[2]
    assert SCHEMA_PATH.is_relative_to(repo_root)
