"""Fail-closed prerequisite audit for the three-store Data Live Gate.

The report deliberately records only presence booleans for credentials. It
does not fetch external APIs, mutate stores, or print secret values.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
sys.path.insert(0, str(LAW_ROOT))

from src.config import Settings  # noqa: E402
from src.ingest.laws import TARGET_LAWS  # noqa: E402

Phase = Literal["assets", "services", "release"]
Connector = Callable[[str, int, float], bool]

REQUIRED_ASSETS = (
    "data/관세청_HS부호 단위별 품목명_20260101.xlsx",
    "data/관세청_HS부호_20260101.xlsx",
    "data/ccct_progress.jsonl",
)


def _tcp_connect(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _host_port(url: str, default_port: int) -> tuple[str, int]:
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise ValueError("service URL must include a hostname")
    return parsed.hostname, parsed.port or default_port


def _runtime_config() -> dict[str, Any]:
    env_file = LAW_ROOT / ".env"
    settings = Settings(_env_file=env_file if env_file.exists() else None)
    qdrant_host, qdrant_port = _host_port(settings.qdrant_url, 6333)
    neo4j_host, neo4j_port = _host_port(settings.neo4j_uri, 7687)
    return {
        "services": {
            "postgres": (settings.postgres_host, settings.postgres_port),
            "qdrant": (qdrant_host, qdrant_port),
            "neo4j": (neo4j_host, neo4j_port),
        },
        "credentials": {
            "law_api_oc_present": _credential_present(settings.law_api_oc),
            "data_go_kr_service_key_present": _credential_present(
                settings.data_go_kr_service_key
            ),
        },
    }


def _credential_present(value: str) -> bool:
    normalized = value.strip().casefold()
    return bool(normalized) and normalized not in {
        "change_me",
        "replace_me",
        "your_service_key_here",
    }


def build_report(
    *,
    phase: Phase,
    runtime: Mapping[str, Any],
    law_root: Path = LAW_ROOT,
    target_law_ids: Sequence[str] = tuple(str(spec["law_id"]) for spec in TARGET_LAWS),
    connector: Connector = _tcp_connect,
    timeout_seconds: float = 1.0,
) -> dict[str, Any]:
    asset_rows = []
    for relative in REQUIRED_ASSETS:
        path = law_root / relative
        asset_rows.append(
            {
                "path": relative,
                "present": path.is_file() and path.stat().st_size > 0,
            }
        )
    assets_ok = all(row["present"] for row in asset_rows)

    raw_dir = law_root / "data" / "raw"
    valid_law_ids: list[str] = []
    missing_law_ids: list[str] = []
    invalid_law_ids: list[str] = []
    for law_id in target_law_ids:
        cache_path = raw_dir / f"law_current_{law_id}.json"
        if not cache_path.is_file() or cache_path.stat().st_size == 0:
            missing_law_ids.append(law_id)
            continue
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            invalid_law_ids.append(law_id)
            continue
        law = payload.get("법령") if isinstance(payload, dict) else None
        basic = law.get("기본정보") if isinstance(law, dict) else None
        actual_law_id = basic.get("법령ID") if isinstance(basic, dict) else None
        articles = law.get("조문") if isinstance(law, dict) else None
        if str(actual_law_id or "").strip() != law_id or not articles:
            invalid_law_ids.append(law_id)
            continue
        valid_law_ids.append(law_id)
    law_cache_complete = len(valid_law_ids) == len(target_law_ids)

    service_rows: dict[str, dict[str, Any]] = {}
    services_required = phase in {"services", "release"}
    if services_required:
        for name, endpoint in runtime["services"].items():
            host, port = endpoint
            service_rows[name] = {
                "host": str(host),
                "port": int(port),
                "reachable": connector(str(host), int(port), timeout_seconds),
            }
    services_ok = not services_required or all(
        row["reachable"] for row in service_rows.values()
    )

    credential_presence = {
        key: bool(value) for key, value in runtime["credentials"].items()
    }
    credentials_required = phase == "release"
    credentials_ok = not credentials_required or all(credential_presence.values())

    failures = []
    if not assets_ok:
        failures.append("REQUIRED_LOCAL_ASSETS_MISSING")
    if not services_ok:
        failures.append("THREE_STORE_SERVICES_UNREACHABLE")
    if not credentials_ok:
        failures.append("EXTERNAL_CREDENTIALS_MISSING")
    if phase == "release" and not law_cache_complete:
        failures.append("TARGET_LAW_CACHE_INCOMPLETE")

    return {
        "schema_version": "1.0",
        "scope": "prerequisites_only",
        "phase": phase,
        "status": "PASS" if not failures else "FAIL",
        "checks": {
            "local_assets": {"ok": assets_ok, "files": asset_rows},
            "law_cache": {
                "expected": len(target_law_ids),
                "valid": len(valid_law_ids),
                "missing_law_ids": missing_law_ids,
                "invalid_law_ids": invalid_law_ids,
                "complete": law_cache_complete,
            },
            "services": {
                "required": services_required,
                "ok": services_ok,
                "endpoints": service_rows,
            },
            "external_credentials": {
                "required": credentials_required,
                "ok": credentials_ok,
                **credential_presence,
            },
        },
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("assets", "services", "release"),
        default="services",
    )
    parser.add_argument("--timeout-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    report = build_report(
        phase=args.phase,
        runtime=_runtime_config(),
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
