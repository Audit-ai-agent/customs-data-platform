from __future__ import annotations

import json
import math
import multiprocessing
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from src import db
from src.db import postgres as postgres_module
from src.db import qdrant as qdrant_module
from src.deadline import DeadlineExceeded, RequestDeadline
from src.model_runtime import (
    DenseModelSupervisor,
    ModelInferenceTimeout,
    ModelRuntimeError,
)
from src.neo4j_runtime import (
    KillableNeo4jQueryExecutor,
    Neo4jQueryTimeout,
    Neo4jRuntimeError,
)
from src.process_ipc import write_bounded_response
from src.production_settings import (
    ProductionConfigurationError,
    ProductionSettings,
    validate_runtime_deadline_settings,
)

_SENSITIVE_CANARIES = {
    "EVIDENCE_SERVICE_TOKEN": "must-not-enter-child",
    "ANTHROPIC_API_KEY": "must-not-enter-child",
    "POSTGRES_PASSWORD": "must-not-enter-child",
    "LAW_API_OC": "must-not-enter-child",
}


def _environment_is_scrubbed() -> bool:
    return all(name not in os.environ for name in _SENSITIVE_CANARIES)


def _model_success_worker(connection: Any) -> None:
    environment_clean = _environment_is_scrubbed()
    connection.send(("ready",))
    try:
        while True:
            message = connection.recv()
            if message == ("close",):
                return
            (
                command,
                request_id,
                _texts,
                response_path,
                response_identity,
                maximum_bytes,
            ) = message
            assert command == "encode"
            vector = [0.0] * 1_024
            vector[0] = 1.0 if environment_clean else -1.0
            payload = json.dumps(
                {"status": "result", "vectors": [vector]},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            write_bounded_response(
                response_path,
                expected_identity=response_identity,
                payload=payload,
                maximum_bytes=maximum_bytes,
            )
            connection.send(("result_ready", request_id))
    finally:
        connection.close()


def _model_hang_worker(connection: Any) -> None:
    connection.send(("ready",))
    connection.recv()
    while True:
        time.sleep(1)


def _model_malformed_worker(connection: Any) -> None:
    connection.send(("ready",))
    message = connection.recv()
    (
        _command,
        request_id,
        _texts,
        response_path,
        response_identity,
        maximum_bytes,
    ) = message
    payload = b'{"status":"result","vectors":[[0.0]]}'
    write_bounded_response(
        response_path,
        expected_identity=response_identity,
        payload=payload,
        maximum_bytes=maximum_bytes,
    )
    connection.send(("result_ready", request_id))
    while True:
        time.sleep(1)


def _neo_success_worker(**kwargs: Any) -> None:
    assert "parameters" not in kwargs
    parameters = json.loads(kwargs["parameters_json"].decode())
    payload = json.dumps(
        {
            "rows": [
                {
                    "environment_clean": _environment_is_scrubbed(),
                    "parameter": parameters["value"],
                }
            ],
            "status": "result",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    write_bounded_response(
        kwargs["response_path"],
        expected_identity=kwargs["response_identity"],
        payload=payload,
        maximum_bytes=kwargs["maximum_response_bytes"],
    )


def _neo_hang_worker(**_kwargs: Any) -> None:
    while True:
        time.sleep(1)


def _neo_abnormal_worker(**_kwargs: Any) -> None:
    os._exit(23)


def _deadline(
    *,
    request: float = 3.0,
    store: float = 1.0,
    model: float = 1.0,
    reserve: float = 0.1,
) -> RequestDeadline:
    return RequestDeadline.start(
        started_at=time.monotonic(),
        request_timeout_seconds=request,
        store_timeout_seconds=store,
        model_timeout_seconds=model,
        completion_reserve_seconds=reserve,
    )


def _child_pids() -> set[int]:
    return {process.pid for process in multiprocessing.active_children() if process.pid is not None}


def _temp_names(prefix: str) -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob(f"{prefix}*.json"))


def test_request_deadline_fake_clock_floor_and_libpq_minimum() -> None:
    now = [100.0]
    deadline = RequestDeadline.start(
        started_at=100.0,
        request_timeout_seconds=5.0,
        store_timeout_seconds=3.7,
        model_timeout_seconds=2.0,
        completion_reserve_seconds=0.25,
        clock=lambda: now[0],
    )
    assert deadline.remaining_seconds() == pytest.approx(5.0)
    assert deadline.work_seconds() == pytest.approx(4.75)
    assert deadline.store_operation_whole_seconds() == 3
    assert deadline.postgres_connect_whole_seconds() == 3

    now[0] = 102.8
    assert deadline.store_operation_whole_seconds() == 1
    with pytest.raises(DeadlineExceeded):
        deadline.postgres_connect_whole_seconds()

    now[0] = 104.76
    with pytest.raises(DeadlineExceeded):
        deadline.assert_work_allowed()


def test_runtime_settings_require_kill_reserves_inside_deadline() -> None:
    token = "deadline-test-token-" + ("x" * 40)
    invalid = ProductionSettings(
        _env_file=None,
        service_token=token,
        transport_mode="direct_tls",
        request_deadline_seconds=5,
        store_operation_timeout_seconds=3,
        store_termination_timeout_seconds=1,
        model_inference_timeout_seconds=1,
        model_termination_timeout_seconds=1,
        shutdown_grace_seconds=5,
    )
    with pytest.raises(ProductionConfigurationError, match="store operation"):
        validate_runtime_deadline_settings(invalid)


class _FastPostgresDeadline:
    completion_reserve_seconds = 0.1

    @staticmethod
    def postgres_connect_whole_seconds() -> int:
        return 2

    @staticmethod
    def store_operation_milliseconds() -> int:
        return 100

    @staticmethod
    def store_operation_seconds() -> float:
        return 0.1

    @staticmethod
    def assert_work_allowed() -> None:
        return None


class _BlackholePostgresConnection:
    def __init__(self) -> None:
        self.closed = False
        self.cancelled = threading.Event()
        self.released = threading.Event()
        self.rollback_calls = 0
        self.commit_calls = 0

    def cancel_safe(self, *, timeout: float) -> None:
        assert 0 < timeout <= 0.1
        self.cancelled.set()

    def close(self) -> None:
        self.closed = True
        self.released.set()

    def rollback(self) -> None:
        self.rollback_calls += 1

    def commit(self) -> None:
        self.commit_calls += 1


def test_postgres_blackhole_is_cancelled_closed_and_watchdog_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _BlackholePostgresConnection()
    captured: dict[str, Any] = {}

    def fake_connect(dsn: str, **kwargs: Any) -> _BlackholePostgresConnection:
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return connection

    monkeypatch.setattr(postgres_module.psycopg.pq, "version", lambda: 170000)
    monkeypatch.setattr(postgres_module.psycopg, "connect", fake_connect)
    monkeypatch.setattr(postgres_module.settings, "postgres_host", "db.internal")
    monkeypatch.setattr(postgres_module.settings, "postgres_hostaddr", "192.0.2.20")

    started = time.monotonic()
    with pytest.raises(DeadlineExceeded, match="PostgreSQL operation"):
        with postgres_module.connect(deadline=_FastPostgresDeadline()) as selected:
            assert selected is connection
            assert connection.released.wait(timeout=1)
    elapsed = time.monotonic() - started

    assert elapsed < 0.8
    assert connection.cancelled.is_set()
    assert connection.closed
    assert captured["kwargs"]["connect_timeout"] == 2
    assert captured["kwargs"]["hostaddr"] == "192.0.2.20"
    assert "statement_timeout=100" in captured["kwargs"]["options"]
    assert not any(
        thread.name == "customs-evidence-postgres-deadline" for thread in threading.enumerate()
    )


def test_postgres_deadline_runtime_rejects_old_libpq_and_dns_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres_module.psycopg.pq, "version", lambda: 169999)
    with pytest.raises(RuntimeError, match="libpq 17"):
        postgres_module.validate_deadline_runtime()

    monkeypatch.setattr(postgres_module.psycopg.pq, "version", lambda: 170000)
    monkeypatch.setattr(postgres_module.settings, "postgres_host", "db.internal")
    monkeypatch.setattr(postgres_module.settings, "postgres_hostaddr", "")
    with pytest.raises(RuntimeError, match="hostaddr"):
        postgres_module.validate_deadline_runtime()


class _BlackholeQdrant:
    def __init__(self) -> None:
        self.closed = threading.Event()
        self.close_calls = 0

    def slow_drip_forever(self) -> None:
        assert self.closed.wait(timeout=3)
        raise TimeoutError("transport closed by wall-clock watchdog")

    def ignore_transport_error_and_return(self) -> None:
        assert self.closed.wait(timeout=3)

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


def test_qdrant_request_client_disables_probe_and_absolute_watchdog_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor: dict[str, Any] = {}

    def fake_qdrant_client(**kwargs: Any) -> object:
        constructor.update(kwargs)
        return object()

    monkeypatch.setattr(qdrant_module, "QdrantClient", fake_qdrant_client)
    qdrant_module.client(timeout_seconds=2)
    assert constructor["timeout"] == 2
    assert constructor["check_compatibility"] is False

    instance = _BlackholeQdrant()
    captured_timeout: list[int] = []

    def factory(*, timeout_seconds: int) -> _BlackholeQdrant:
        captured_timeout.append(timeout_seconds)
        return instance

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="wall-clock watchdog"):
        with qdrant_module.bounded_client(
            _deadline(request=3, store=1),
            client_factory=factory,
        ) as client:
            client.slow_drip_forever()
    elapsed = time.monotonic() - started

    assert 0.8 <= elapsed < 1.8
    assert captured_timeout == [1]
    assert instance.close_calls == 1
    assert not any(
        thread.name == "customs-evidence-qdrant-deadline" for thread in threading.enumerate()
    )

    ignored = _BlackholeQdrant()
    with pytest.raises(DeadlineExceeded, match="Qdrant operation"):
        with qdrant_module.bounded_client(
            _deadline(request=3, store=1),
            client_factory=lambda *, timeout_seconds: ignored,
        ) as client:
            client.ignore_transport_error_and_return()


def _neo_executor(
    worker: Any,
    *,
    maximum_request_bytes: int = 1_048_576,
) -> KillableNeo4jQueryExecutor:
    return KillableNeo4jQueryExecutor(
        uri="bolt://127.0.0.1:7687",
        user="explicit-user",
        password="explicit-password",
        termination_timeout_seconds=0.1,
        maximum_request_bytes=maximum_request_bytes,
        worker_target=worker,
    )


def test_neo4j_canonical_ipc_scrubs_environment_and_returns_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _SENSITIVE_CANARIES.items():
        monkeypatch.setenv(name, value)
    executor = _neo_executor(_neo_success_worker)
    try:
        rows = executor.run(
            "RETURN $value AS value",
            {"value": 17},
            _deadline(request=10, store=5),
        )
    finally:
        executor.close()
    assert rows == [{"environment_clean": True, "parameter": 17}]
    assert executor.active_process_count == 0


def test_neo4j_hang_is_terminated_reaped_and_temp_removed() -> None:
    before_children = _child_pids()
    before_temp = _temp_names("customs-evidence-neo4j-")
    executor = _neo_executor(_neo_hang_worker)
    started = time.monotonic()
    try:
        with pytest.raises(Neo4jQueryTimeout):
            executor.run(
                "RETURN 1",
                {},
                _deadline(request=2, store=0.2),
            )
    finally:
        executor.close()
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert executor.active_process_count == 0
    assert _child_pids() <= before_children
    assert _temp_names("customs-evidence-neo4j-") == before_temp


def test_neo4j_close_kills_inflight_child_and_recovers_capacity() -> None:
    executor = _neo_executor(_neo_hang_worker)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            executor.run(
                "RETURN 1",
                {},
                _deadline(request=10, store=5),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke, name="neo4j-deadline-test")
    thread.start()
    cutoff = time.monotonic() + 2
    while executor.active_process_count != 1 and time.monotonic() < cutoff:
        time.sleep(0.01)
    assert executor.active_process_count == 1

    started = time.monotonic()
    executor.close()
    thread.join(timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert not thread.is_alive()
    assert errors
    assert executor.active_process_count == 0
    with pytest.raises(Neo4jRuntimeError, match="closed"):
        executor.run("RETURN 1", {}, _deadline())


def test_neo4j_rejects_combined_query_parameter_limit_and_abnormal_exit() -> None:
    limited = _neo_executor(_neo_success_worker, maximum_request_bytes=20)
    try:
        with pytest.raises(Neo4jRuntimeError, match="IPC byte limit"):
            limited.run("RETURN $value", {"value": 1}, _deadline())
    finally:
        limited.close()

    abnormal = _neo_executor(_neo_abnormal_worker)
    try:
        with pytest.raises(Neo4jRuntimeError, match="abnormally"):
            abnormal.run("RETURN 1", {}, _deadline(request=10, store=5))
    finally:
        abnormal.close()


def _model_supervisor(worker: Any) -> DenseModelSupervisor:
    return DenseModelSupervisor(
        startup_timeout_seconds=15,
        termination_timeout_seconds=0.1,
        worker_target=worker,
    )


def test_model_success_validates_shape_scrubs_environment_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in _SENSITIVE_CANARIES.items():
        monkeypatch.setenv(name, value)
    before_children = _child_pids()
    supervisor = _model_supervisor(_model_success_worker)
    supervisor.start()
    vector = supervisor.encode(
        ["bounded model request"],
        _deadline(request=2, model=0.5),
    )
    assert len(vector) == 1
    assert len(vector[0]) == 1_024
    assert vector[0][0] == 1.0
    assert all(math.isfinite(value) for value in vector[0])
    supervisor.close()
    assert _child_pids() <= before_children


def test_model_hang_is_killed_reaped_broken_and_temp_removed() -> None:
    before_children = _child_pids()
    before_temp = _temp_names("customs-evidence-model-")
    supervisor = _model_supervisor(_model_hang_worker)
    supervisor.start()
    started = time.monotonic()
    with pytest.raises(ModelInferenceTimeout):
        supervisor.encode(
            ["hang"],
            _deadline(request=2, model=0.2),
        )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    with pytest.raises(ModelRuntimeError, match="unavailable"):
        supervisor.assert_ready()
    with pytest.raises(ModelRuntimeError, match="unavailable"):
        supervisor.encode(["second"], _deadline())
    supervisor.close()
    assert _child_pids() <= before_children
    assert _temp_names("customs-evidence-model-") == before_temp


def test_model_malformed_response_terminates_worker_and_fails_closed() -> None:
    supervisor = _model_supervisor(_model_malformed_worker)
    supervisor.start()
    with pytest.raises(ModelRuntimeError, match="invalid vectors"):
        supervisor.encode(
            ["malformed"],
            _deadline(request=2, model=0.5),
        )
    with pytest.raises(ModelRuntimeError, match="unavailable"):
        supervisor.assert_ready()
    supervisor.close()


def test_model_lock_wait_and_inference_share_one_subdeadline() -> None:
    supervisor = _model_supervisor(_model_hang_worker)
    supervisor.start()
    first_errors: list[BaseException] = []

    def first_call() -> None:
        try:
            supervisor.encode(
                ["first"],
                _deadline(request=3, model=0.8),
            )
        except BaseException as exc:
            first_errors.append(exc)

    thread = threading.Thread(target=first_call, name="model-deadline-test")
    thread.start()
    time.sleep(0.1)
    started = time.monotonic()
    with pytest.raises(ModelInferenceTimeout, match="admission"):
        supervisor.encode(
            ["queued"],
            _deadline(request=2, model=0.2),
        )
    elapsed = time.monotonic() - started
    thread.join(timeout=2)
    supervisor.close()

    assert elapsed < 0.5
    assert not thread.is_alive()
    assert first_errors


def test_model_close_interrupts_concurrent_inference_without_lock_deadlock() -> None:
    supervisor = _model_supervisor(_model_hang_worker)
    supervisor.start()
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            supervisor.encode(
                ["inflight"],
                _deadline(request=10, model=5),
            )
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke, name="model-close-test")
    thread.start()
    time.sleep(0.2)
    started = time.monotonic()
    supervisor.close()
    thread.join(timeout=2)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert not thread.is_alive()
    assert errors
