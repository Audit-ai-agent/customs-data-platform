"""Fail-closed prerequisite audit for the local three-store Data workspace.

The check never fetches external APIs, mutates stores, or reads credentials.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LAW_ROOT = ROOT / "law_repository"
sys.path.insert(0, str(LAW_ROOT))

from src.config import Settings  # noqa: E402

Phase = Literal["assets", "services"]
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
    }


def build_report(
    *,
    phase: Phase,
    runtime: Mapping[str, Any],
    law_root: Path = LAW_ROOT,
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

    service_rows: dict[str, dict[str, Any]] = {}
    services_required = phase == "services"
    if services_required:
        for name, endpoint in runtime["services"].items():
            host, port = endpoint
            service_rows[name] = {
                "host": str(host),
                "port": int(port),
                "reachable": connector(str(host), int(port), timeout_seconds),
            }
    services_ok = not services_required or all(row["reachable"] for row in service_rows.values())

    failures = []
    if not assets_ok:
        failures.append("REQUIRED_LOCAL_ASSETS_MISSING")
    if not services_ok:
        failures.append("THREE_STORE_SERVICES_UNREACHABLE")
    return {
        "schema_version": "1.0",
        "scope": "prerequisites_only",
        "phase": phase,
        "status": "PASS" if not failures else "FAIL",
        "checks": {
            "local_assets": {"ok": assets_ok, "files": asset_rows},
            "services": {
                "required": services_required,
                "ok": services_ok,
                "endpoints": service_rows,
            },
        },
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("assets", "services"),
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
