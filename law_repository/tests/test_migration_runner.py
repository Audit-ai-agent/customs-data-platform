from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts.migrate import (
    MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    discover_migrations,
)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self._one: tuple[Any, ...] | None = None
        self._all: list[tuple[Any, ...]] = []
        self.rowcount = -1

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(query.split())
        self.connection.statements.append(normalized)
        self.rowcount = -1
        if normalized.startswith("SELECT pg_try_advisory_lock"):
            self._one = (self.connection.lock_available,)
            if self.connection.lock_available:
                self.connection.locked = True
            return
        if normalized.startswith("SELECT pg_advisory_unlock"):
            self._one = (self.connection.locked,)
            self.connection.locked = False
            return
        if normalized.startswith("SELECT filename, checksum_sha256, byte_size"):
            self._all = [
                (filename, checksum, size)
                for filename, (checksum, size) in sorted(self.connection.applied.items())
            ]
            return
        if normalized.startswith("UPDATE schema_migrations SET checksum_sha256"):
            assert params is not None
            checksum, size, filename = params
            self.connection.applied[str(filename)] = (str(checksum), int(size))
            self.rowcount = 1
            return
        if normalized.startswith("INSERT INTO schema_migrations"):
            assert params is not None
            filename, checksum, size = params
            self.connection.applied[str(filename)] = (str(checksum), int(size))
            self.rowcount = 1
            return
        if normalized.startswith(("CREATE TABLE IF NOT EXISTS schema_migrations", "ALTER TABLE")):
            return
        if self.connection.fail_sql_fragment and self.connection.fail_sql_fragment in query:
            raise RuntimeError("injected migration SQL failure")
        self.connection.applied_sql.append(query)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._all


class FakeConnection:
    def __init__(self) -> None:
        self.applied: dict[str, tuple[str | None, int | None]] = {}
        self.statements: list[str] = []
        self.applied_sql: list[str] = []
        self.lock_available = True
        self.locked = False
        self.commits = 0
        self.rollbacks = 0
        self.fail_sql_fragment: str | None = None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_repository_migrations_are_contiguous_and_checksum_attested() -> None:
    migrations = discover_migrations()

    assert [item.number for item in migrations] == [1, 2, 3, 4, 5, 6]
    assert [item.filename for item in migrations] == [
        "001_init.sql",
        "002_parent_article_pk.sql",
        "003_ccct_requirements.sql",
        "004_law_history_provenance.sql",
        "005_law_history_version_identity.sql",
        "006_law_history_official_uri_constraint.sql",
    ]
    assert all(
        hashlib.sha256(item.path.read_bytes()).hexdigest() == item.checksum_sha256
        for item in migrations
    )
    assert all(item.byte_size > 0 for item in migrations)

    identity = migrations[-2].sql
    assert "version_key" in identity
    assert "law_versions_law_id_mst_key" in identity
    assert "law_version_sources_law_id_version_key_fkey" in identity

    uri_constraint = migrations[-1].sql
    assert "law_version_sources_official_uri_check" in uri_constraint
    assert "www[.]law[.]go[.]kr" in uri_constraint


def test_discovery_rejects_gap_and_non_utf8(tmp_path: Path) -> None:
    (tmp_path / "001_first.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "003_gap.sql").write_text("SELECT 3;", encoding="utf-8")
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(tmp_path)

    (tmp_path / "003_gap.sql").unlink()
    (tmp_path / "002_binary.sql").write_bytes(b"\xff")
    with pytest.raises(MigrationError, match="strict UTF-8"):
        discover_migrations(tmp_path)


def test_apply_current_profile_history_is_idempotent() -> None:
    migrations = discover_migrations()
    connection = FakeConnection()
    messages: list[str] = []

    apply_migrations(connection, migrations, output=messages.append)
    first_sql_count = len(connection.applied_sql)
    apply_migrations(connection, migrations, output=messages.append)

    assert set(connection.applied) == {item.filename for item in migrations}
    assert len(connection.applied_sql) == first_sql_count
    assert connection.locked is False
    assert any("checksum verified" in message for message in messages)


def test_checksumless_legacy_history_requires_profile_reset() -> None:
    migrations = discover_migrations()
    connection = FakeConnection()
    connection.applied[migrations[0].filename] = (None, None)

    with pytest.raises(MigrationError, match="unsupported prior profile; reset/rebuild"):
        apply_migrations(connection, migrations, output=lambda _message: None)

    assert connection.applied_sql == []
    assert connection.locked is False


def test_applied_migration_tamper_requires_profile_reset_before_new_sql() -> None:
    migrations = discover_migrations()
    connection = FakeConnection()
    connection.applied[migrations[0].filename] = ("0" * 64, migrations[0].byte_size)

    with pytest.raises(MigrationError, match="not current.*reset/rebuild"):
        apply_migrations(connection, migrations, output=lambda _message: None)

    assert connection.applied_sql == []
    assert connection.locked is False


def test_removed_applied_migration_fails_closed(tmp_path: Path) -> None:
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name != "001_init.sql":
            shutil.copy2(source, tmp_path / source.name)
    connection = FakeConnection()
    connection.applied["001_init.sql"] = (
        discover_migrations()[0].checksum_sha256,
        discover_migrations()[0].byte_size,
    )

    # Discovery itself rejects the numbering gap before database access.
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(tmp_path)


def test_advisory_lock_contention_executes_no_migration() -> None:
    connection = FakeConnection()
    connection.lock_available = False

    with pytest.raises(MigrationError, match="another migration writer"):
        apply_migrations(connection, discover_migrations(), output=lambda _message: None)

    assert connection.applied_sql == []
    assert connection.locked is False


def test_retired_generation_migration_requires_profile_reset() -> None:
    migrations = discover_migrations()
    connection = FakeConnection()
    connection.applied["004_knowledge_generations.sql"] = ("0" * 64, 1)

    with pytest.raises(MigrationError, match="unsupported entries.*reset/rebuild"):
        apply_migrations(connection, migrations, output=lambda _message: None)

    assert connection.applied_sql == []
