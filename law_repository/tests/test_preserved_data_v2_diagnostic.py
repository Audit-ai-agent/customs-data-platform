from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tools import run_preserved_data_v2_diagnostic as runner

_TOKEN = "preserved-readonly-diagnostic-token-0123456789ABCDEFG"
_REVISION = "a" * 40


class _Cursor:
    def __init__(self, *, fail_generation_load: bool = False) -> None:
        self._query = ""
        self._fail_generation_load = fail_generation_load

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _parameters: object = None) -> None:
        self._query = " ".join(query.split()).casefold()
        if self._fail_generation_load and "from knowledge_generation_active_pointer" in self._query:
            raise RuntimeError("generation tables are absent")

    def fetchone(self) -> tuple[Any, ...] | None:
        if "show transaction_read_only" in self._query:
            return ("on",)
        if "count(distinct law_id)" in self._query:
            return (3,)
        if "count(*) from law_articles" in self._query:
            return (1_007,)
        return None

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "information_schema.tables" in self._query:
            return []
        return []


class _Connection:
    def __init__(self, *, fail_generation_load: bool = False) -> None:
        self._fail_generation_load = fail_generation_load

    def cursor(self) -> _Cursor:
        return _Cursor(fail_generation_load=self._fail_generation_load)


def _connection_factory(_deadline: object = None):
    @contextmanager
    def managed():
        yield _Connection(fail_generation_load=True)

    return managed()


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "service-token"
    path.write_text(_TOKEN + "\n", encoding="ascii")
    return path


def test_readonly_audit_records_incomplete_corpus_and_generation_tables() -> None:
    audit = runner.audit_preserved_postgres(
        provider_revision=_REVISION,
        connection_factory=_connection_factory,
    )

    assert audit.target_law_count == 3
    assert audit.article_count == 1_007
    assert audit.generation_tables_present == ()
    assert audit.generation_tables_missing == runner.GENERATION_TABLES
    assert audit.to_wire()["database_write_attempted"] is False
    assert audit.to_wire()["release_ready"] is False


def test_parser_requires_explicit_ack_and_loopback(tmp_path: Path) -> None:
    common = [
        "--production-context",
        str(tmp_path),
        "--service-token-file",
        str(tmp_path / "token"),
        "--audit-output",
        str(tmp_path / "audit.json"),
        "--host",
        "127.0.0.1",
        "--port",
        "18011",
    ]
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(common)
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(
            [
                "--test-only-preserved-readonly-diagnostic",
                *common[:-4],
                "--host",
                "0.0.0.0",
                "--port",
                "18011",
            ]
        )


def test_real_v2_boundary_returns_503_and_records_zero_retrievals(tmp_path: Path) -> None:
    audit_output = tmp_path / "external" / "preserved-audit.json"
    app = runner.create_preserved_data_diagnostic_application(
        production_context=tmp_path / "context",
        service_token_file=_token_file(tmp_path),
        audit_output=audit_output,
        host="127.0.0.1",
        port=18011,
        connection_factory=_connection_factory,
        provider_lock_loader=lambda _root: SimpleNamespace(provider_revision=_REVISION),
        provider_identity_loader=lambda _root: object(),
    )

    with TestClient(
        app,
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 51001),
    ) as client:
        assert client.get("/internal/rag/v2/health/ready").status_code == 401
        ready = client.get(
            "/internal/rag/v2/health/ready",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        metrics = client.get(
            "/internal/rag/v2/metrics",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )

    assert ready.status_code == 503
    assert 'route="ready",method="GET",status_class="5xx"' in metrics.text
    assert 'route="retrieve"' not in metrics.text
    assert app.state.test_only_production_shell is True
    assert app.state.test_only_runtime_conformance is False
    assert app.state.test_only_preserved_readonly_diagnostic is True
    assert app.state.release_ready is False
    assert audit_output.exists()
    assert '"target_law_count": 3' in audit_output.read_text(encoding="utf-8")


def test_audit_output_refuses_repository_and_existing_paths(tmp_path: Path) -> None:
    audit = runner.PreservedDataAudit(
        provider_revision=_REVISION,
        target_law_count=3,
        article_count=1_007,
        generation_tables_present=(),
    )
    with pytest.raises(runner.PreservedDataDiagnosticError, match="outside"):
        runner.write_audit_json(audit, runner.ROOT / "forbidden-audit.json")

    output = tmp_path / "audit.json"
    runner.write_audit_json(audit, output)
    with pytest.raises(runner.PreservedDataDiagnosticError, match="new regular"):
        runner.write_audit_json(audit, output)
