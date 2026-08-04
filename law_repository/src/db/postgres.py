"""Small local PostgreSQL connection helper."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from src.config import settings


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Yield one transaction and always close its local connection."""

    connection = psycopg.connect(settings.postgres_dsn)
    try:
        yield connection
        connection.commit()
    except Exception:
        if not connection.closed:
            connection.rollback()
        raise
    finally:
        connection.close()
