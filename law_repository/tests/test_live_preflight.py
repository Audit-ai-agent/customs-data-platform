from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.config import Settings
from tools.live_preflight import REQUIRED_ASSETS, build_report


def _assets(root: Path) -> None:
    for relative in REQUIRED_ASSETS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")


def _law_cache(root: Path, law_ids: tuple[str, ...]) -> None:
    raw_dir = root / "data" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for law_id in law_ids:
        (raw_dir / f"law_current_{law_id}.json").write_text(
            json.dumps(
                {
                    "법령": {
                        "기본정보": {"법령ID": law_id},
                        "조문": {"조문단위": [{"조문번호": "1"}]},
                    }
                }
            ),
            encoding="utf-8",
        )


def _runtime(*, credentials: bool = False) -> dict:
    return {
        "services": {
            "postgres": ("127.0.0.1", 55432),
            "qdrant": ("127.0.0.1", 6333),
            "neo4j": ("127.0.0.1", 7687),
        },
        "credentials": {
            "law_api_oc_present": credentials,
            "data_go_kr_service_key_present": credentials,
        },
    }


def test_assets_phase_is_offline_and_does_not_require_secrets(tmp_path: Path) -> None:
    _assets(tmp_path)

    def unexpected_connect(_host: str, _port: int, _timeout: float) -> bool:
        raise AssertionError("assets phase must not contact a service")

    report = build_report(
        phase="assets",
        runtime=_runtime(),
        law_root=tmp_path,
        target_law_ids=("001556", "002421"),
        connector=unexpected_connect,
    )

    assert report["status"] == "PASS"
    assert report["checks"]["services"]["required"] is False
    assert report["checks"]["external_credentials"]["required"] is False


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


def test_release_phase_records_presence_only_and_requires_both_credentials(
    tmp_path: Path,
) -> None:
    _assets(tmp_path)
    _law_cache(tmp_path, ("001556", "002421"))
    runtime = _runtime(credentials=False)
    runtime["credentials"]["law_api_oc_present"] = True

    report = build_report(
        phase="release",
        runtime=runtime,
        law_root=tmp_path,
        target_law_ids=("001556", "002421"),
        connector=lambda _host, _port, _timeout: True,
    )

    assert report["status"] == "FAIL"
    assert report["failures"] == ["EXTERNAL_CREDENTIALS_MISSING"]
    serialized = json.dumps(report)
    assert "secret" not in serialized.casefold()
    assert set(report["checks"]["external_credentials"]) == {
        "required",
        "ok",
        "law_api_oc_present",
        "data_go_kr_service_key_present",
    }


def test_release_phase_fails_when_any_target_law_cache_is_missing_or_invalid(
    tmp_path: Path,
) -> None:
    _assets(tmp_path)
    _law_cache(tmp_path, ("001556",))
    invalid = tmp_path / "data" / "raw" / "law_current_002421.json"
    invalid.write_text("not-json", encoding="utf-8")

    report = build_report(
        phase="release",
        runtime=_runtime(credentials=True),
        law_root=tmp_path,
        target_law_ids=("001556", "002421", "006392"),
        connector=lambda _host, _port, _timeout: True,
    )

    assert report["status"] == "FAIL"
    assert report["failures"] == ["TARGET_LAW_CACHE_INCOMPLETE"]
    assert report["checks"]["law_cache"] == {
        "expected": 3,
        "valid": 1,
        "missing_law_ids": ["006392"],
        "invalid_law_ids": ["002421"],
        "complete": False,
    }


def test_release_phase_passes_only_with_services_credentials_assets_and_cache(
    tmp_path: Path,
) -> None:
    _assets(tmp_path)
    _law_cache(tmp_path, ("001556", "002421"))

    report = build_report(
        phase="release",
        runtime=_runtime(credentials=True),
        law_root=tmp_path,
        target_law_ids=("001556", "002421"),
        connector=lambda _host, _port, _timeout: True,
    )

    assert report["status"] == "PASS"
    assert report["failures"] == []


def test_cli_rejects_non_positive_timeout_without_traceback() -> None:
    script = Path(__file__).resolve().parents[2] / "tools" / "live_preflight.py"
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

    runtime_settings = Settings(_env_file=None)

    assert runtime_settings.postgres_port == 55432


def test_local_client_urls_follow_compose_port_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "QDRANT_URL",
        "QDRANT_HTTP_PORT",
        "NEO4J_URI",
        "NEO4J_BOLT_PORT",
    ):
        monkeypatch.delenv(name, raising=False)

    runtime_settings = Settings(
        _env_file=None,
        qdrant_http_port=16444,
        neo4j_bolt_port=17687,
    )

    assert runtime_settings.qdrant_url == "http://localhost:16444"
    assert runtime_settings.neo4j_uri == "bolt://localhost:17687"


def test_remote_service_urls_are_not_rewritten_by_local_ports() -> None:
    runtime_settings = Settings(
        _env_file=None,
        qdrant_url="https://qdrant.example.test",
        qdrant_http_port=16444,
        neo4j_uri="neo4j+s://neo4j.example.test",
        neo4j_bolt_port=17687,
    )

    assert runtime_settings.qdrant_url == "https://qdrant.example.test"
    assert runtime_settings.neo4j_uri == "neo4j+s://neo4j.example.test"
