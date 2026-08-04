from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.config import Settings
from tools.local_preflight import REQUIRED_ASSETS, build_report


def _assets(root: Path) -> None:
    for relative in REQUIRED_ASSETS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")


def _runtime() -> dict:
    return {
        "services": {
            "postgres": ("127.0.0.1", 55432),
            "qdrant": ("127.0.0.1", 6333),
            "neo4j": ("127.0.0.1", 7687),
        },
    }


def test_assets_phase_is_offline_and_does_not_contact_a_service(tmp_path: Path) -> None:
    _assets(tmp_path)

    def unexpected_connect(_host: str, _port: int, _timeout: float) -> bool:
        raise AssertionError("assets phase must not contact a service")

    report = build_report(
        phase="assets",
        runtime=_runtime(),
        law_root=tmp_path,
        connector=unexpected_connect,
    )

    assert report["status"] == "PASS"
    assert report["checks"]["services"]["required"] is False
    assert set(report["checks"]) == {"local_assets", "services"}


def test_services_phase_fails_closed_when_any_store_is_unreachable(tmp_path: Path) -> None:
    _assets(tmp_path)

    report = build_report(
        phase="services",
        runtime=_runtime(),
        law_root=tmp_path,
        connector=lambda _host, port, _timeout: port != 6333,
    )

    assert report["status"] == "FAIL"
    assert report["failures"] == ["THREE_STORE_SERVICES_UNREACHABLE"]
    assert report["checks"]["services"]["endpoints"]["qdrant"]["reachable"] is False


def test_cli_rejects_non_positive_timeout_without_traceback() -> None:
    script = Path(__file__).resolve().parents[2] / "tools" / "local_preflight.py"
    result = subprocess.run(
        [sys.executable, str(script), "--timeout-seconds", "0"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "must be greater than zero" in result.stderr
    assert "Traceback" not in result.stderr


def test_default_postgres_port_matches_isolated_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    assert Settings(_env_file=None).postgres_port == 55432


def test_local_client_urls_follow_compose_port_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("QDRANT_URL", "QDRANT_HTTP_PORT", "NEO4J_URI", "NEO4J_BOLT_PORT"):
        monkeypatch.delenv(name, raising=False)

    runtime_settings = Settings(
        _env_file=None,
        qdrant_http_port=16444,
        neo4j_bolt_port=17687,
    )
    assert runtime_settings.qdrant_url == "http://localhost:16444"
    assert runtime_settings.neo4j_uri == "bolt://localhost:17687"
