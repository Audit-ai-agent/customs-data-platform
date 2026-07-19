"""Killable Neo4j query boundary for the production Evidence runtime.

Neo4j's transaction timeout is enforced by the server and its public Python
driver exposes connect, pool-acquisition, and write timeouts.  It does not
offer a general client read timeout for a connection that goes black-hole
after accepting a query.  Each production query therefore runs in a short
child process.  The parent kills and reaps that process before returning from
a timeout, so no Bolt thread or admission slot can leak indefinitely.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
from collections.abc import Callable, Mapping
from multiprocessing.context import BaseContext
from threading import Condition, Lock
from typing import Any, NoReturn, Protocol

from src.deadline import DeadlineExceeded, RequestDeadline
from src.process_ipc import (
    FileIdentity,
    close_and_unlink,
    file_identity,
    read_bounded_response,
    write_bounded_response,
)
from src.process_security import scrub_sensitive_environment


class Neo4jRuntimeError(RuntimeError):
    """A bounded Neo4j worker failed or returned an invalid response."""


class Neo4jQueryTimeout(DeadlineExceeded):
    """A Neo4j worker was killed at its hard client deadline."""


class Neo4jQueryExecutor(Protocol):
    def run(
        self,
        query: str,
        parameters: Mapping[str, Any],
        deadline: RequestDeadline,
    ) -> list[dict[str, Any]]: ...


def _neo4j_query_worker(
    *,
    uri: str,
    user: str,
    password: str,
    database: str | None,
    query: str,
    timeout_seconds: float,
    maximum_rows: int,
    parameters_json: bytes,
    response_path: str,
    response_identity: FileIdentity,
    maximum_response_bytes: int,
) -> None:
    payload: dict[str, Any]
    try:
        from neo4j import GraphDatabase, Query

        parameters = json.loads(parameters_json.decode("utf-8"))
        if not isinstance(parameters, dict):
            raise ValueError("Neo4j parameters must be an object")
        driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            connection_timeout=timeout_seconds,
            connection_acquisition_timeout=timeout_seconds,
            connection_write_timeout=timeout_seconds,
            liveness_check_timeout=timeout_seconds,
            max_connection_pool_size=1,
            keep_alive=False,
        )
        try:
            session_config: dict[str, Any] = {
                "connection_acquisition_timeout": timeout_seconds,
            }
            if database:
                session_config["database"] = database
            with driver.session(**session_config) as session:
                rows: list[dict[str, Any]] = []
                result = session.run(
                    Query(query, timeout=timeout_seconds),
                    **parameters,
                )
                for record in result:
                    if len(rows) >= maximum_rows:
                        raise ValueError("Neo4j query row limit exceeded")
                    rows.append(dict(record))
        finally:
            driver.close()
        payload = {"status": "result", "rows": rows}
    except BaseException as exc:
        payload = {"status": "error", "code": type(exc).__name__}
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        serialized = b'{"code":"serialization","status":"error"}'
    if len(serialized) > maximum_response_bytes:
        serialized = b'{"code":"response_size","status":"error"}'
    write_bounded_response(
        response_path,
        expected_identity=response_identity,
        payload=serialized,
        maximum_bytes=maximum_response_bytes,
    )


def _run_neo4j_worker_scrubbed(
    worker_target: Callable[..., None],
    worker_kwargs: dict[str, Any],
) -> None:
    scrub_sensitive_environment()
    worker_target(**worker_kwargs)


def _fatal_process_exit(code: int) -> NoReturn:
    os._exit(code)


class KillableNeo4jQueryExecutor:
    """Execute one Bolt query per child and reap it before every return."""

    def __init__(
        self,
        *,
        uri: str,
        user: str,
        password: str,
        termination_timeout_seconds: float,
        database: str | None = None,
        maximum_rows: int = 100_000,
        maximum_request_bytes: int = 1_048_576,
        maximum_response_bytes: int = 4_194_304,
        start_method: str = "spawn",
        process_context: BaseContext | None = None,
        worker_target: Callable[..., None] = _neo4j_query_worker,
        fatal_exit: Callable[[int], NoReturn] = _fatal_process_exit,
    ) -> None:
        if termination_timeout_seconds <= 0:
            raise ValueError("Neo4j termination timeout must be positive")
        if maximum_rows <= 0:
            raise ValueError("Neo4j maximum row count must be positive")
        if maximum_request_bytes <= 0 or maximum_response_bytes <= 0:
            raise ValueError("Neo4j IPC byte limits must be positive")
        self._uri = uri
        self._user = user
        self._password = password
        self._database = database
        self._termination_timeout = termination_timeout_seconds
        self._maximum_rows = maximum_rows
        self._maximum_request_bytes = maximum_request_bytes
        self._maximum_response_bytes = maximum_response_bytes
        self._context: Any = process_context or multiprocessing.get_context(start_method)
        self._worker_target = worker_target
        self._fatal_exit = fatal_exit
        self._condition = Condition()
        self._reap_lock = Lock()
        self._active_processes: set[Any] = set()
        self._closed = False

    def _query_seconds(self, deadline: RequestDeadline) -> float:
        cleanup_reserve = 2 * self._termination_timeout
        available = deadline.work_seconds() - cleanup_reserve
        timeout = min(deadline.store_timeout_seconds, available)
        if timeout < 0.001:
            raise Neo4jQueryTimeout("insufficient request budget for killable Neo4j query")
        return timeout

    def _terminate_and_reap(self, process: Any) -> None:
        with self._reap_lock:
            try:
                alive = process.is_alive()
            except ValueError:
                return
            if alive:
                process.terminate()
                process.join(self._termination_timeout)
            if process.is_alive():
                process.kill()
                process.join(self._termination_timeout)
            if process.is_alive():
                self._fatal_exit(71)

    def _start_registered(self, process: Any) -> None:
        with self._condition:
            if self._closed:
                raise Neo4jRuntimeError("Neo4j runtime executor is closed")
            process.start()
            self._active_processes.add(process)

    def _unregister(self, process: Any) -> None:
        with self._condition:
            self._active_processes.discard(process)
            self._condition.notify_all()

    @property
    def active_process_count(self) -> int:
        with self._condition:
            return len(self._active_processes)

    def run(
        self,
        query: str,
        parameters: Mapping[str, Any],
        deadline: RequestDeadline,
    ) -> list[dict[str, Any]]:
        if not query.strip() or len(query) > 100_000:
            raise Neo4jRuntimeError("Neo4j query is outside the bounded contract")
        try:
            parameters_json = json.dumps(
                dict(parameters),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise Neo4jRuntimeError("Neo4j parameters are not canonical JSON") from exc
        query_bytes = query.encode("utf-8")
        if len(query_bytes) + len(parameters_json) > self._maximum_request_bytes:
            raise Neo4jRuntimeError("Neo4j parameters exceed the IPC byte limit")
        timeout = self._query_seconds(deadline)
        response_fd, response_name = tempfile.mkstemp(
            prefix="customs-evidence-neo4j-",
            suffix=".json",
        )
        response_identity: FileIdentity = file_identity(response_fd)
        worker_kwargs = {
            "uri": self._uri,
            "user": self._user,
            "password": self._password,
            "database": self._database,
            "query": query,
            "timeout_seconds": timeout,
            "maximum_rows": self._maximum_rows,
            "parameters_json": parameters_json,
            "response_path": response_name,
            "response_identity": response_identity,
            "maximum_response_bytes": self._maximum_response_bytes,
        }
        process = self._context.Process(
            target=_run_neo4j_worker_scrubbed,
            args=(self._worker_target, worker_kwargs),
            name="customs-evidence-neo4j-query",
            daemon=True,
        )
        started = False
        try:
            self._start_registered(process)
            started = True
            process.join(timeout)
            if process.is_alive():
                self._terminate_and_reap(process)
                raise Neo4jQueryTimeout("Neo4j query exceeded its hard deadline")
            if process.exitcode != 0:
                raise Neo4jRuntimeError("Neo4j query worker exited abnormally")
            try:
                raw_response = read_bounded_response(
                    response_fd,
                    expected_identity=response_identity,
                    maximum_bytes=self._maximum_response_bytes,
                )
                message = json.loads(raw_response.decode("utf-8"))
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                raise Neo4jRuntimeError("Neo4j query worker response failed") from exc
            rows = message.get("rows") if isinstance(message, dict) else None
            if (
                not isinstance(message, dict)
                or set(message) != {"status", "rows"}
                or message.get("status") != "result"
                or not isinstance(rows, list)
            ):
                raise Neo4jRuntimeError("Neo4j query worker failed closed")
            if len(rows) > self._maximum_rows or any(not isinstance(row, dict) for row in rows):
                raise Neo4jRuntimeError("Neo4j query worker returned invalid rows")
            deadline.assert_work_allowed()
            return rows
        finally:
            close_and_unlink(response_fd, response_name)
            if started:
                try:
                    if process.is_alive():
                        self._terminate_and_reap(process)
                finally:
                    self._unregister(process)
            try:
                process.close()
            except (OSError, ValueError):
                pass

    def close(self) -> None:
        """Stop accepting queries and kill every currently registered child."""

        with self._condition:
            self._closed = True
            active = tuple(self._active_processes)
        for process in active:
            self._terminate_and_reap(process)
        # Query threads wake immediately after their child is reaped and own
        # final descriptor/temp cleanup.  Bound this coordination wait.
        with self._condition:
            self._condition.wait_for(
                lambda: not self._active_processes,
                timeout=self._termination_timeout,
            )


__all__ = [
    "KillableNeo4jQueryExecutor",
    "Neo4jQueryExecutor",
    "Neo4jQueryTimeout",
    "Neo4jRuntimeError",
]
