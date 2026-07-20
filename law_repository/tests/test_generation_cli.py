from __future__ import annotations

from pathlib import Path

import pytest

from src.generation_cli import _load_json_object, build_parser, main


def test_operator_cli_exposes_complete_release_lifecycle() -> None:
    parser = build_parser()
    subparser_action = next(
        action
        for action in parser._actions
        if action.dest == "command"  # type: ignore[attr-defined]
    )
    assert set(subparser_action.choices) == {
        "manifest",
        "build",
        "validate",
        "seal",
        "promote",
        "rollback",
        "reconcile",
        "active",
    }


@pytest.mark.parametrize(
    "payload",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b"[]",
        b"\xff",
    ],
)
def test_operator_json_boundary_is_strict(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "input.json"
    path.write_bytes(payload)
    with pytest.raises((ValueError, UnicodeDecodeError)):
        _load_json_object(path)


def test_promote_requires_exact_generation_confirmation(capsys) -> None:
    result = main(
        [
            "promote",
            "a" * 64,
            "--confirm-generation-id",
            "b" * 64,
            "--qdrant-revision",
            "qdrant-1.15.4",
            "--neo4j-revision",
            "neo4j-5",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert '"error_type": "ValueError"' in captured.err
    assert "a" * 64 not in captured.err
    assert captured.out == ""
