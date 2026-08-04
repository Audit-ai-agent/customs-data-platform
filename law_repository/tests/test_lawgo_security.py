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


@pytest.fixture
def initialized_session(monkeypatch: pytest.MonkeyPatch) -> requests.Session:
    """Preserve the client's lazy session construction in every test."""

    session = requests.Session()
    monkeypatch.setattr(lawgo, "_session", session)
    return session


def test_get_requires_real_credential_before_network(
    monkeypatch: pytest.MonkeyPatch,
    initialized_session: requests.Session,
) -> None:
    monkeypatch.setattr(lawgo.settings, "law_api_oc", "")
    monkeypatch.setattr(
        initialized_session,
        "get",
        lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(lawgo.LawApiError, match="LAW_API_CREDENTIAL_UNAVAILABLE"):
        lawgo.get("lawService.do", target="law", ID="001556")


def test_official_client_has_no_checkout_raw_cache_writer() -> None:
    """Only the bounded UAT builder owns raw official source persistence."""

    assert not hasattr(lawgo, "RAW_DIR")
    assert not hasattr(lawgo, "fetch_current_law")
    assert not hasattr(lawgo, "fetch_law_by_mst")


def test_get_rejects_redirect_without_leaking_credential(
    monkeypatch: pytest.MonkeyPatch,
    initialized_session: requests.Session,
) -> None:
    secret = "secret-credential-for-test"
    response = _Response(status=302)
    monkeypatch.setattr(lawgo.settings, "law_api_oc", secret)
    monkeypatch.setattr(initialized_session, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(lawgo.LawApiError) as captured:
        lawgo.get("lawService.do", target="law", ID="001556")

    assert captured.value.code == "LAW_API_REDIRECT_REJECTED"
    assert secret not in str(captured.value)
    assert response.closed


def test_get_uses_bounded_stream_and_returns_normalized_json(
    monkeypatch: pytest.MonkeyPatch,
    initialized_session: requests.Session,
) -> None:
    response = _Response(
        chunks=(json.dumps({"법 령": {"값": 1}}, ensure_ascii=False).encode("utf-8"),)
    )
    observed: dict[str, object] = {}
    monkeypatch.setattr(lawgo.settings, "law_api_oc", "approved-test-credential")

    def fake_get(_url, **kwargs):
        observed.update(kwargs)
        return response

    monkeypatch.setattr(initialized_session, "get", fake_get)

    assert lawgo.get("lawService.do", target="law", ID="001556") == {"법령": {"값": 1}}
    assert observed["allow_redirects"] is False
    assert observed["stream"] is True
    assert response.closed


def test_get_stops_when_stream_exceeds_bound(
    monkeypatch: pytest.MonkeyPatch,
    initialized_session: requests.Session,
) -> None:
    response = _Response(chunks=(b"x" * (lawgo.MAX_RESPONSE_BYTES + 1),))
    monkeypatch.setattr(lawgo.settings, "law_api_oc", "approved-test-credential")
    monkeypatch.setattr(initialized_session, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(lawgo.LawApiError, match="LAW_API_RESPONSE_TOO_LARGE"):
        lawgo.get("lawService.do", target="law", ID="001556")

    assert response.closed


def test_transport_failure_is_retried_but_error_is_content_free(
    monkeypatch: pytest.MonkeyPatch,
    initialized_session: requests.Session,
) -> None:
    calls = 0
    secret = "approved-test-credential"
    monkeypatch.setattr(lawgo.settings, "law_api_oc", secret)
    monkeypatch.setattr(lawgo, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(lawgo.time, "sleep", lambda _seconds: None)

    def fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise requests.ConnectionError(f"url?OC={secret}")

    monkeypatch.setattr(initialized_session, "get", fail)

    with pytest.raises(lawgo.LawApiError) as captured:
        lawgo.get("lawSearch.do", target="law", query="관세법")

    assert calls == 2
    assert captured.value.code == "LAW_API_TRANSPORT_FAILURE"
    assert secret not in str(captured.value)
