from __future__ import annotations

import json
from typing import Iterable

import pytest
import requests

from src import lawgo


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        chunks: Iterable[bytes] = (),
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status
        self._chunks = tuple(chunks)
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size: int):
        assert chunk_size == 64 * 1024
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def test_get_requires_real_credential_before_network(monkeypatch) -> None:
    monkeypatch.setattr(lawgo.settings, "law_api_oc", "")
    monkeypatch.setattr(
        lawgo._session,
        "get",
        lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(lawgo.LawApiError, match="LAW_API_CREDENTIAL_UNAVAILABLE"):
        lawgo.get("lawService.do", target="law", ID="001556")


def test_get_rejects_redirect_without_leaking_credential(monkeypatch) -> None:
    secret = "secret-credential-for-test"
    response = _Response(status=302)
    monkeypatch.setattr(lawgo.settings, "law_api_oc", secret)
    monkeypatch.setattr(lawgo._session, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(lawgo.LawApiError) as captured:
        lawgo.get("lawService.do", target="law", ID="001556")

    assert captured.value.code == "LAW_API_REDIRECT_REJECTED"
    assert secret not in str(captured.value)
    assert response.closed


def test_get_uses_bounded_stream_and_returns_normalized_json(monkeypatch) -> None:
    response = _Response(
        chunks=(json.dumps({"법 령": {"값": 1}}, ensure_ascii=False).encode("utf-8"),)
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(lawgo.settings, "law_api_oc", "approved-test-credential")

    def fake_get(_url, **kwargs):
        observed.update(kwargs)
        return response

    monkeypatch.setattr(lawgo._session, "get", fake_get)

    assert lawgo.get("lawService.do", target="law", ID="001556") == {"법령": {"값": 1}}
    assert observed["allow_redirects"] is False
    assert observed["stream"] is True
    assert response.closed


def test_get_stops_when_stream_exceeds_bound(monkeypatch) -> None:
    response = _Response(chunks=(b"x" * (lawgo.MAX_RESPONSE_BYTES + 1),))
    monkeypatch.setattr(lawgo.settings, "law_api_oc", "approved-test-credential")
    monkeypatch.setattr(lawgo._session, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(lawgo.LawApiError, match="LAW_API_RESPONSE_TOO_LARGE"):
        lawgo.get("lawService.do", target="law", ID="001556")

    assert response.closed


def test_transport_failure_is_retried_but_error_is_content_free(monkeypatch) -> None:
    calls = 0
    secret = "approved-test-credential"
    monkeypatch.setattr(lawgo.settings, "law_api_oc", secret)
    monkeypatch.setattr(lawgo, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(lawgo.time, "sleep", lambda _seconds: None)

    def fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise requests.ConnectionError(f"url?OC={secret}")

    monkeypatch.setattr(lawgo._session, "get", fail)

    with pytest.raises(lawgo.LawApiError) as captured:
        lawgo.get("lawSearch.do", target="law", query="관세법")

    assert calls == 2
    assert captured.value.code == "LAW_API_TRANSPORT_FAILURE"
    assert secret not in str(captured.value)
