"""Killable process boundary for production dense-model inference.

PyTorch inference cannot be safely cancelled when it runs in an ASGI worker
thread.  Returning HTTP 504 while such a thread continues forever would leak
both work and admission capacity.  Production therefore owns one warmed child
process.  A timed-out inference is terminated (then hard-killed if needed)
*before* the request worker returns.  The supervisor remains failed closed
until the service is restarted.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from multiprocessing.context import BaseContext
from threading import Lock
from typing import Any, NoReturn

from src.deadline import DeadlineExceeded, RequestDeadline
from src.process_ipc import (
    FileIdentity,
    close_and_unlink,
    file_identity,
    read_bounded_response,
    write_bounded_response,
)
from src.process_security import scrub_sensitive_environment

_DENSE_VECTOR_SIZE = 1_024
_MODEL_RESPONSE_MAX_BYTES = 131_072


class ModelRuntimeError(RuntimeError):
    """The supervised model worker is unavailable or returned invalid data."""


class ModelInferenceTimeout(DeadlineExceeded):
    """Model work was killed at its hard inference deadline."""


def _dense_worker_main(connection: Any) -> None:
    """Warm and serve the pinned dense model inside the killable child."""

    try:
        from src.embed.dense import encode

        # Load immutable model bytes and execute one real kernel before the
        # service begins admitting requests.
        encode(["bounded model readiness probe"])
        connection.send(("ready",))
    except BaseException as exc:
        try:
            connection.send(("startup_error", type(exc).__name__))
        finally:
            connection.close()
        return

    try:
        while True:
            message = connection.recv()
            if message == ("close",):
                return
            if not isinstance(message, tuple) or len(message) != 6 or message[0] != "encode":
                return
            _, request_id, texts, response_path, response_identity, maximum_bytes = message
            try:
                if (
                    not isinstance(request_id, str)
                    or not isinstance(texts, tuple)
                    or not isinstance(response_path, str)
                    or not isinstance(response_identity, tuple)
                    or len(response_identity) != 2
                    or not all(isinstance(value, int) for value in response_identity)
                    or maximum_bytes != _MODEL_RESPONSE_MAX_BYTES
                ):
                    raise ValueError("dense model request protocol is invalid")
                result = encode(list(texts))
                if getattr(result, "shape", None) != (len(texts), _DENSE_VECTOR_SIZE):
                    raise ValueError("dense model output shape is invalid")
                vectors = result.tolist()
                if any(
                    not isinstance(vector, list)
                    or len(vector) != _DENSE_VECTOR_SIZE
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        for value in vector
                    )
                    for vector in vectors
                ):
                    raise ValueError("dense model output values are invalid")
                payload = {"status": "result", "vectors": vectors}
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
            if len(serialized) > _MODEL_RESPONSE_MAX_BYTES:
                serialized = b'{"code":"response_size","status":"error"}'
            write_bounded_response(
                response_path,
                expected_identity=response_identity,
                payload=serialized,
                maximum_bytes=_MODEL_RESPONSE_MAX_BYTES,
            )
            connection.send(("result_ready", request_id))
    except (EOFError, BrokenPipeError, OSError):
        return
    finally:
        connection.close()


def _run_dense_worker_scrubbed(
    worker_target: Callable[[Any], None],
    connection: Any,
) -> None:
    scrub_sensitive_environment()
    worker_target(connection)


def _fatal_process_exit(code: int) -> NoReturn:
    # If the OS cannot kill the isolated model worker, exiting the service is
    # safer than releasing admission while unbounded sensitive work survives.
    os._exit(code)


class DenseModelSupervisor:
    """Own one warmed model worker and enforce kill-before-return semantics."""

    def __init__(
        self,
        *,
        startup_timeout_seconds: float,
        termination_timeout_seconds: float,
        start_method: str = "spawn",
        worker_target: Callable[[Any], None] = _dense_worker_main,
        process_context: BaseContext | None = None,
        fatal_exit: Callable[[int], NoReturn] = _fatal_process_exit,
    ) -> None:
        if startup_timeout_seconds <= 0 or termination_timeout_seconds <= 0:
            raise ValueError("model process timeouts must be positive")
        self._startup_timeout = startup_timeout_seconds
        self._termination_timeout = termination_timeout_seconds
        self._context: Any = process_context or multiprocessing.get_context(start_method)
        self._worker_target = worker_target
        self._fatal_exit = fatal_exit
        self._lifecycle_lock = Lock()
        self._call_lock = Lock()
        self._process: Any | None = None
        self._connection: Any | None = None
        self._ready = False
        self._broken = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._ready and self._process is not None and self._process.is_alive():
                return
            if self._broken:
                raise ModelRuntimeError("model supervisor is failed closed")
            parent, child = self._context.Pipe(duplex=True)
            process = self._context.Process(
                target=_run_dense_worker_scrubbed,
                args=(self._worker_target, child),
                name="customs-evidence-dense-model",
                daemon=True,
            )
            self._process = process
            self._connection = parent
            process.start()
            child.close()
            if not parent.poll(self._startup_timeout):
                self._terminate_locked()
                raise ModelRuntimeError("model worker startup timed out")
            try:
                message = parent.recv()
            except (EOFError, BrokenPipeError, OSError) as exc:
                self._terminate_locked()
                raise ModelRuntimeError("model worker exited during startup") from exc
            if message != ("ready",) or not process.is_alive():
                self._terminate_locked()
                raise ModelRuntimeError("model worker failed to initialize")
            self._ready = True

    def assert_ready(self) -> None:
        with self._lifecycle_lock:
            if (
                self._broken
                or not self._ready
                or self._process is None
                or not self._process.is_alive()
            ):
                raise ModelRuntimeError("model supervisor is unavailable")

    @staticmethod
    def _validate_texts(texts: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(texts)
        if (
            not normalized
            or len(normalized) != 1
            or any(
                not isinstance(text, str) or not text or len(text) > 32_768 for text in normalized
            )
        ):
            raise ModelRuntimeError("model input is outside the bounded runtime contract")
        return normalized

    def encode(self, texts: list[str], deadline: RequestDeadline) -> list[list[float]]:
        normalized = self._validate_texts(texts)
        # Lock admission and inference share one sub-deadline.  A queued call
        # cannot wait for one full model cap and then receive a second cap.
        operation_budget = self._inference_seconds(deadline)
        operation_started = time.monotonic()
        if not self._call_lock.acquire(timeout=operation_budget):
            raise ModelInferenceTimeout("model worker admission timed out")
        try:
            timeout = operation_budget - (time.monotonic() - operation_started)
            if timeout < 0.001:
                raise ModelInferenceTimeout("model worker admission consumed its deadline")
            with self._lifecycle_lock:
                if (
                    self._broken
                    or not self._ready
                    or self._process is None
                    or self._connection is None
                    or not self._process.is_alive()
                ):
                    raise ModelRuntimeError("model supervisor is unavailable")
                connection = self._connection
                request_id = uuid.uuid4().hex
                response_fd, response_path = tempfile.mkstemp(
                    prefix="customs-evidence-model-",
                    suffix=".json",
                )
                response_identity: FileIdentity = file_identity(response_fd)
                try:
                    connection.send(
                        (
                            "encode",
                            request_id,
                            normalized,
                            response_path,
                            response_identity,
                            _MODEL_RESPONSE_MAX_BYTES,
                        ),
                    )
                except (BrokenPipeError, EOFError, OSError) as exc:
                    close_and_unlink(response_fd, response_path)
                    self._terminate_locked()
                    raise ModelRuntimeError("model worker request channel failed") from exc

            try:
                if not connection.poll(timeout):
                    self._break_worker()
                    raise ModelInferenceTimeout("model inference exceeded its hard deadline")
                try:
                    message = connection.recv()
                except (EOFError, BrokenPipeError, OSError) as exc:
                    self._break_worker()
                    raise ModelRuntimeError("model worker response channel failed") from exc
                if message != ("result_ready", request_id):
                    self._break_worker()
                    raise ModelRuntimeError("model worker returned an invalid response")
                raw_response = read_bounded_response(
                    response_fd,
                    expected_identity=response_identity,
                    maximum_bytes=_MODEL_RESPONSE_MAX_BYTES,
                )
                payload = json.loads(raw_response.decode("utf-8"))
                vectors = payload.get("vectors") if isinstance(payload, dict) else None
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"status", "vectors"}
                    or payload.get("status") != "result"
                    or not isinstance(vectors, list)
                    or len(vectors) != len(normalized)
                    or any(
                        not isinstance(vector, list)
                        or len(vector) != _DENSE_VECTOR_SIZE
                        or any(
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(float(value))
                            for value in vector
                        )
                        for vector in vectors
                    )
                ):
                    self._break_worker()
                    raise ModelRuntimeError("model worker returned invalid vectors")
                deadline.assert_work_allowed()
                return [[float(value) for value in vector] for vector in vectors]
            except ModelInferenceTimeout:
                raise
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                self._break_worker()
                raise ModelRuntimeError("model worker response failed") from exc
            except RuntimeError as exc:
                if isinstance(exc, ModelRuntimeError):
                    raise
                self._break_worker()
                raise ModelRuntimeError("model worker response failed") from exc
            finally:
                close_and_unlink(response_fd, response_path)
        finally:
            self._call_lock.release()

    def _inference_seconds(self, deadline: RequestDeadline) -> float:
        # The request must still own enough time to terminate and, if needed,
        # hard-kill the child before the dependency-work cutoff.
        cleanup_reserve = 2 * self._termination_timeout
        available = deadline.work_seconds() - cleanup_reserve
        timeout = min(deadline.model_timeout_seconds, available)
        if timeout < 0.001:
            raise ModelInferenceTimeout("insufficient request budget for killable model inference")
        return timeout

    def _break_worker(self) -> None:
        with self._lifecycle_lock:
            self._terminate_locked()

    def _terminate_locked(self) -> None:
        self._ready = False
        self._broken = True
        process = self._process
        connection = self._connection
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if process is not None and process.is_alive():
            process.terminate()
            process.join(self._termination_timeout)
        if process is not None and process.is_alive():
            process.kill()
            process.join(self._termination_timeout)
        if process is not None and process.is_alive():
            self._fatal_exit(70)
        if process is not None:
            try:
                process.close()
            except (OSError, ValueError):
                pass
        self._process = None
        self._connection = None

    def close(self) -> None:
        # Shutdown must not wait behind an inference lock.  Terminate/kill is
        # bounded to two termination intervals and wakes an encode poller.
        with self._lifecycle_lock:
            self._terminate_locked()


__all__ = [
    "DenseModelSupervisor",
    "ModelInferenceTimeout",
    "ModelRuntimeError",
]
