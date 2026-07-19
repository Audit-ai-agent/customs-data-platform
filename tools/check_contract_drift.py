"""Fail when Evidence v1 bodies or runtime v2 readiness drift from schemas.

The JSON Schema is the provider-owned source of truth.  This check resolves
local references and compares every wire-relevant JSON Schema keyword while
ignoring documentation-only titles and descriptions.
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LAW_REPOSITORY = ROOT / "law_repository"
SCHEMA_PATH = ROOT / "contracts" / "evidence" / "v1" / "rag_retrieval_v1.schema.json"
READINESS_SCHEMA_PATH = ROOT / "contracts" / "evidence" / "v1" / "readiness_v1.schema.json"
RUNTIME_READINESS_SCHEMA_PATH = ROOT / "contracts" / "evidence" / "v2" / "readiness_v2.schema.json"
sys.path.insert(0, str(LAW_REPOSITORY))

from fastapi import FastAPI  # noqa: E402
from src.runtime_api import configure_runtime_v2_application  # noqa: E402
from src.webapp import app  # noqa: E402


COMPONENTS = {
    "RetrieveItem": "RetrieveItem",
    "RetrieveRequest": "RetrieveRequest",
    "RetrievalError": "EvidenceError",
    "Evidence": "Evidence",
    "RetrieveResult": "ItemEvidenceResult",
    "RetrieveResponse": "RetrieveResponse",
}
READINESS_COMPONENTS = {
    "ProviderBuildIdentity": "ProviderBuildIdentity",
    "LawCorpusReadiness": "LawCorpusReadiness",
    "LawIndexReadiness": "LawIndexReadiness",
    "ReadinessResponse": "ReadinessResponse",
}
RUNTIME_READINESS_COMPONENTS = {
    "EvidenceRuntimeReleaseIdentity": "EvidenceRuntimeReleaseIdentity",
    "EvidenceRuntimeReadiness": "EvidenceRuntimeReadiness",
}


class _SchemaOnlyRuntimeLoader:
    def acquire(self):
        raise RuntimeError("schema-only application")

    def assert_unchanged(self, _lease):
        raise RuntimeError("schema-only application")


runtime_app = configure_runtime_v2_application(
    FastAPI(title="Evidence runtime v2 schema check"),
    runtime_release_loader=_SchemaOnlyRuntimeLoader(),  # type: ignore[arg-type]
    backend_factory=lambda _lease: None,  # type: ignore[arg-type,return-value]
    test_only_conformance=True,
)

WIRE_KEYWORDS = {
    "type",
    "const",
    "enum",
    "required",
    "additionalProperties",
    "properties",
    "items",
    "anyOf",
    "oneOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "uniqueItems",
    "default",
}


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"Only local references are supported: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part.replace("~1", "/").replace("~0", "~")]
    return node


def _signature(rule: Any, root: dict[str, Any]) -> Any:
    if not isinstance(rule, dict):
        return rule
    if "$ref" in rule:
        return _signature(_resolve_ref(rule["$ref"], root), root)

    signature: dict[str, Any] = {}
    for key in sorted(WIRE_KEYWORDS & rule.keys()):
        value = rule[key]
        if key == "properties":
            signature[key] = {
                name: _signature(child, root) for name, child in sorted(value.items())
            }
        elif key in {"items", "additionalProperties"} and isinstance(value, dict):
            signature[key] = _signature(value, root)
        elif key in {"anyOf", "oneOf"}:
            children = [_signature(child, root) for child in value]
            signature[key] = sorted(
                children,
                key=lambda child: json.dumps(child, ensure_ascii=False, sort_keys=True),
            )
        elif key in {"required", "enum"}:
            signature[key] = sorted(value)
        else:
            signature[key] = value
    # ``const`` already fixes the JSON value and therefore its type. Pydantic
    # emits the redundant type keyword while the canonical schema omits it.
    if "const" in signature:
        signature.pop("type", None)
    return signature


def _compare_components(
    *,
    canonical_path: Path,
    canonical: dict[str, Any],
    openapi: dict[str, Any],
    component_mapping: dict[str, str],
) -> list[str]:
    components = openapi["components"]["schemas"]
    errors: list[str] = []
    for canonical_name, openapi_name in component_mapping.items():
        expected = _signature(canonical["$defs"][canonical_name], canonical)
        actual = _signature(components[openapi_name], openapi)
        if expected == actual:
            continue
        expected_text = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True)
        actual_text = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True)
        diff = "\n".join(
            difflib.unified_diff(
                expected_text.splitlines(),
                actual_text.splitlines(),
                fromfile=f"canonical:{canonical_path.name}:{canonical_name}",
                tofile=f"openapi:{openapi_name}",
                lineterm="",
            )
        )
        errors.append(diff)
    return errors


def check_contract_drift() -> list[str]:
    openapi = app.openapi()
    runtime_openapi = runtime_app.openapi()
    checks = (
        (SCHEMA_PATH, COMPONENTS, openapi),
        (READINESS_SCHEMA_PATH, READINESS_COMPONENTS, openapi),
        (SCHEMA_PATH, COMPONENTS, runtime_openapi),
        (
            RUNTIME_READINESS_SCHEMA_PATH,
            RUNTIME_READINESS_COMPONENTS,
            runtime_openapi,
        ),
    )
    errors: list[str] = []
    for path, mapping, selected_openapi in checks:
        canonical = json.loads(path.read_text(encoding="utf-8"))
        errors.extend(
            _compare_components(
                canonical_path=path,
                canonical=canonical,
                openapi=selected_openapi,
                component_mapping=mapping,
            )
        )
    return errors


def main() -> int:
    errors = check_contract_drift()
    if errors:
        print("Evidence v1 contract drift detected:\n" + "\n\n".join(errors))
        return 1
    print(
        "Evidence contracts match OpenAPI: "
        f"{SCHEMA_PATH.relative_to(ROOT)}, {READINESS_SCHEMA_PATH.relative_to(ROOT)}, "
        f"{RUNTIME_READINESS_SCHEMA_PATH.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
