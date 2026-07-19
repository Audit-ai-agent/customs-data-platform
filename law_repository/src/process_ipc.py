"""Bounded file IPC primitives for killable runtime child processes.

The parent keeps the descriptor returned by ``mkstemp`` open until it has read
the response.  The child may write only to that already-created file and must
prove that the path still identifies the same file before writing.  This
avoids following a replaced path and gives both sides an exact byte ceiling.
"""

from __future__ import annotations

import os
from typing import TypeAlias

FileIdentity: TypeAlias = tuple[int, int]


def file_identity(descriptor: int) -> FileIdentity:
    stat = os.fstat(descriptor)
    return int(stat.st_dev), int(stat.st_ino)


def write_bounded_response(
    path: str,
    *,
    expected_identity: FileIdentity,
    payload: bytes,
    maximum_bytes: int,
) -> None:
    if not payload or len(payload) > maximum_bytes:
        raise ValueError("child response is outside the byte limit")
    descriptor = os.open(
        path,
        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if file_identity(descriptor) != expected_identity:
            raise RuntimeError("child response file identity changed")
        os.ftruncate(descriptor, 0)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("child response write made no progress")
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_open_file(
    descriptor: int,
    *,
    expected_identity: FileIdentity,
    payload: bytes,
    maximum_bytes: int,
) -> None:
    """Write bounded parent-owned request bytes to an already-open file."""

    if not payload or len(payload) > maximum_bytes:
        raise ValueError("parent request is outside the byte limit")
    if file_identity(descriptor) != expected_identity:
        raise RuntimeError("parent request file identity changed")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("parent request write made no progress")
        written += count
    os.fsync(descriptor)


def read_bounded_response(
    descriptor: int,
    *,
    expected_identity: FileIdentity,
    maximum_bytes: int,
) -> bytes:
    before = os.fstat(descriptor)
    if (int(before.st_dev), int(before.st_ino)) != expected_identity:
        raise RuntimeError("child response file identity changed")
    if before.st_size <= 0 or before.st_size > maximum_bytes:
        raise ValueError("child response size is invalid")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = maximum_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    after = os.fstat(descriptor)
    if (
        (int(after.st_dev), int(after.st_ino)) != expected_identity
        or after.st_size != before.st_size
        or len(payload) != before.st_size
    ):
        raise RuntimeError("child response changed while reading")
    return payload


def read_bounded_path(
    path: str,
    *,
    expected_identity: FileIdentity,
    maximum_bytes: int,
) -> bytes:
    """Read one existing request path without following a symbolic link."""

    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return read_bounded_response(
            descriptor,
            expected_identity=expected_identity,
            maximum_bytes=maximum_bytes,
        )
    finally:
        os.close(descriptor)


def close_and_unlink(descriptor: int, path: str) -> None:
    """Best-effort cleanup that never masks the original runtime result."""

    try:
        os.close(descriptor)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


__all__ = [
    "FileIdentity",
    "close_and_unlink",
    "file_identity",
    "read_bounded_path",
    "read_bounded_response",
    "write_bounded_response",
    "write_open_file",
]
