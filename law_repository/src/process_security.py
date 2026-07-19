"""Security helpers shared by killable production child processes.

``multiprocessing`` children inherit the parent process environment before
their target starts.  The Evidence service token and unrelated provider
credentials are not needed by either the local dense-model worker or the
Neo4j query worker.  Remove those values at the first line of the child target,
before importing model or database libraries.

Connection values that a worker actually needs are passed as explicit process
arguments.  This function deliberately removes values, never returns or logs
them.
"""

from __future__ import annotations

import os

_SENSITIVE_ENVIRONMENT_FRAGMENTS = (
    "API_KEY",
    "APIKEY",
    "AUTH_TOKEN",
    "BEARER",
    "CREDENTIAL",
    "DATABASE_URL",
    "DSN",
    "LAW_API_OC",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "SERVICE_KEY",
    "TOKEN",
)


def scrub_sensitive_environment() -> None:
    """Delete inherited credentials before a child imports runtime code."""

    for name in tuple(os.environ):
        normalized = name.upper()
        if any(fragment in normalized for fragment in _SENSITIVE_ENVIRONMENT_FRAGMENTS):
            os.environ.pop(name, None)


__all__ = ["scrub_sensitive_environment"]
