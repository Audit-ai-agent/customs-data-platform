"""Dependency-free JSON Schema subset validation plus domain invariants.

The canonical wire shape lives in
``contracts/evidence/v1/rag_retrieval_v1.schema.json``.
This module deliberately uses only the Python standard library so a clean
checkout can validate fixtures and live Evidence Service responses before the
service's own dependency stack is installed.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_PATH = ROOT / "contracts" / "evidence" / "v1" / "rag_retrieval_v1.schema.json"


class ContractViolation(ValueError):
    """Raised when a request or response violates the v1 wire contract."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def load_schema(path: Path | str = DEFAULT_SCHEMA_PATH) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def stable_evidence_id(source_record_id: str, version_hash: str) -> str:
    """Return the contract's stable evidence ID for one source version."""

    canonical = f"{source_record_id}\x1f{version_hash}".encode("utf-8")
    return "ev_" + hashlib.sha256(canonical).hexdigest()


def validate_request(payload: Any, schema: dict[str, Any] | None = None) -> None:
    schema = schema or load_schema()
    errors: list[str] = []
    _validate_schema(payload, schema["$defs"]["RetrieveRequest"], schema, "$", errors)
    if not errors:
        _validate_request_invariants(payload, errors)
    if errors:
        raise ContractViolation(errors)


def validate_response(payload: Any, schema: dict[str, Any] | None = None) -> None:
    schema = schema or load_schema()
    errors: list[str] = []
    _validate_schema(payload, schema["$defs"]["RetrieveResponse"], schema, "$", errors)
    if not errors:
        _validate_response_invariants(payload, errors)
    if errors:
        raise ContractViolation(errors)


def validate_message(payload: Any, schema: dict[str, Any] | None = None) -> str:
    """Validate an unknown message and return ``request`` or ``response``."""

    schema = schema or load_schema()
    request_errors: list[str] = []
    response_errors: list[str] = []
    try:
        validate_request(payload, schema)
        return "request"
    except ContractViolation as exc:
        request_errors = exc.errors
    try:
        validate_response(payload, schema)
        return "response"
    except ContractViolation as exc:
        response_errors = exc.errors
    raise ContractViolation(
        [
            "message matches neither RetrieveRequest nor RetrieveResponse",
            f"request: {'; '.join(request_errors)}",
            f"response: {'; '.join(response_errors)}",
        ]
    )


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"only local schema references are supported: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def _validate_schema(
    value: Any,
    rule: dict[str, Any],
    root: dict[str, Any],
    path: str,
    errors: list[str],
) -> None:
    if "$ref" in rule:
        _validate_schema(value, _resolve_ref(rule["$ref"], root), root, path, errors)
        return

    if "anyOf" in rule:
        branch_errors = []
        for branch in rule["anyOf"]:
            candidate: list[str] = []
            _validate_schema(value, branch, root, path, candidate)
            if not candidate:
                return
            branch_errors.append(candidate)
        errors.append(f"{path}: does not match any allowed shape")
        return

    if "oneOf" in rule:
        matches = 0
        for branch in rule["oneOf"]:
            candidate: list[str] = []
            _validate_schema(value, branch, root, path, candidate)
            matches += not candidate
        if matches != 1:
            errors.append(f"{path}: must match exactly one allowed shape (matched {matches})")
        return

    if "const" in rule and value != rule["const"]:
        errors.append(f"{path}: expected constant {rule['const']!r}")
        return
    if "enum" in rule and value not in rule["enum"]:
        errors.append(f"{path}: {value!r} is not one of {rule['enum']!r}")
        return

    expected_type = rule.get("type")
    if expected_type is not None and not _is_type(value, expected_type):
        errors.append(f"{path}: expected {expected_type}, got {type(value).__name__}")
        return

    if isinstance(value, dict):
        required = rule.get("required", [])
        for name in required:
            if name not in value:
                errors.append(f"{path}: missing required property {name!r}")

        properties = rule.get("properties", {})
        additional = rule.get("additionalProperties", True)
        for name, child in value.items():
            child_path = f"{path}.{name}"
            if name in properties:
                _validate_schema(child, properties[name], root, child_path, errors)
            elif additional is False:
                errors.append(f"{child_path}: additional property is not allowed")
            elif isinstance(additional, dict):
                _validate_schema(child, additional, root, child_path, errors)

        if len(value) < rule.get("minProperties", 0):
            errors.append(f"{path}: requires at least {rule['minProperties']} properties")
        if "maxProperties" in rule and len(value) > rule["maxProperties"]:
            errors.append(f"{path}: allows at most {rule['maxProperties']} properties")

    if isinstance(value, list):
        if len(value) < rule.get("minItems", 0):
            errors.append(f"{path}: requires at least {rule['minItems']} items")
        if "maxItems" in rule and len(value) > rule["maxItems"]:
            errors.append(f"{path}: allows at most {rule['maxItems']} items")
        if rule.get("uniqueItems"):
            encoded = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                errors.append(f"{path}: items must be unique")
        item_rule = rule.get("items")
        if item_rule:
            for index, item in enumerate(value):
                _validate_schema(item, item_rule, root, f"{path}[{index}]", errors)

    if isinstance(value, str):
        if len(value) < rule.get("minLength", 0):
            errors.append(f"{path}: string is shorter than {rule['minLength']}")
        if "maxLength" in rule and len(value) > rule["maxLength"]:
            errors.append(f"{path}: string is longer than {rule['maxLength']}")
        if "pattern" in rule and re.fullmatch(rule["pattern"], value) is None:
            errors.append(f"{path}: value does not match {rule['pattern']!r}")
        if "format" in rule and not _valid_format(value, rule["format"]):
            errors.append(f"{path}: invalid {rule['format']} value")

    if _is_number(value):
        if "minimum" in rule and value < rule["minimum"]:
            errors.append(f"{path}: must be >= {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            errors.append(f"{path}: must be <= {rule['maximum']}")


def _is_type(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_is_type(value, candidate) for candidate in expected)
    checks = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: _is_number(value),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }
    return checks[expected]()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_format(value: str, format_name: str) -> bool:
    try:
        if format_name == "date":
            date.fromisoformat(value)
            return bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
        if format_name == "date-time":
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return True
        if format_name == "uri":
            return bool(urlparse(value).scheme)
    except ValueError:
        return False
    return True


def _validate_request_invariants(payload: dict[str, Any], errors: list[str]) -> None:
    item_ids = [item["item_id"] for item in payload["items"]]
    if len(item_ids) != len(set(item_ids)):
        errors.append("$.items: item_id values must be unique")


def _validate_response_invariants(payload: dict[str, Any], errors: list[str]) -> None:
    results = payload["results"]
    item_ids = [result["item_id"] for result in results]
    if len(item_ids) != len(set(item_ids)):
        errors.append("$.results: item_id values must be unique")

    if payload["status"] == "ok":
        if payload["knowledge_snapshot_id"] is None:
            errors.append("$.knowledge_snapshot_id: required when status is ok")
        if payload.get("errors", []) or any(
            result["coverage"] != "complete" or result["errors"] for result in results
        ):
            errors.append("$.status: ok requires complete, error-free response and results")
    elif payload["status"] == "partial":
        if not payload.get("errors", []) and not any(
            result["coverage"] != "complete" or result["errors"] for result in results
        ):
            errors.append("$.status: partial requires an incomplete or errored result")
    elif payload["status"] == "failed":
        if not payload.get("errors", []) and not any(result["errors"] for result in results):
            errors.append("$.status: failed requires a response or result error")

    seen_evidence: dict[tuple[str, str], str] = {}
    for result_index, result in enumerate(results):
        path = f"$.results[{result_index}]"
        resolution = result["hs_resolution"]
        resolved = result["resolved_hsk10"]
        candidates = result["candidate_hsk10s"]
        if resolution in {"exact", "unique_descendant"}:
            if resolved is None:
                errors.append(f"{path}.resolved_hsk10: required for {resolution}")
            elif candidates and resolved not in candidates:
                errors.append(f"{path}.candidate_hsk10s: must contain resolved_hsk10")
        elif resolution == "ambiguous":
            if resolved is not None or len(candidates) < 2:
                errors.append(f"{path}: ambiguous requires null resolved_hsk10 and >=2 candidates")
        elif resolved is not None or candidates:
            errors.append(f"{path}: {resolution} requires null resolved_hsk10 and no candidates")

        evidence_ids: set[str] = set()
        finding_requirements = 0
        for evidence_index, evidence in enumerate(result["evidence"]):
            evidence_path = f"{path}.evidence[{evidence_index}]"
            if evidence["item_id"] != result["item_id"]:
                errors.append(f"{evidence_path}.item_id: must equal parent result item_id")
            if evidence["evidence_id"] in evidence_ids:
                errors.append(f"{evidence_path}.evidence_id: duplicate within result")
            evidence_ids.add(evidence["evidence_id"])

            expected_id = stable_evidence_id(evidence["source_record_id"], evidence["version_hash"])
            if evidence["evidence_id"] != expected_id:
                errors.append(
                    f"{evidence_path}.evidence_id: must be SHA-256 of source_record_id and version_hash"
                )
            source_key = (evidence["source_record_id"], evidence["version_hash"])
            previous_id = seen_evidence.setdefault(source_key, evidence["evidence_id"])
            if previous_id != evidence["evidence_id"]:
                errors.append(f"{evidence_path}.evidence_id: unstable source-version mapping")

            if evidence["source_type"] == "customs_requirement":
                required = (
                    evidence["law_id"],
                    evidence["law_name"],
                    evidence["valid_from"],
                    evidence["matched_hs_code"],
                    evidence["required_document_code"],
                    evidence["required_document_name"],
                )
                if any(value is None for value in required) or not evidence["agencies"]:
                    errors.append(
                        f"{evidence_path}: customs_requirement requires law, date, document and agencies"
                    )
                if evidence["eligibility"] == "finding":
                    finding_requirements += 1

        if result["requirements_found"] and finding_requirements == 0:
            errors.append(f"{path}: requirements_found requires finding-eligible customs evidence")
        if not result["requirements_found"] and finding_requirements:
            errors.append(
                f"{path}: requirements_found=false conflicts with finding customs evidence"
            )
