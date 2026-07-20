from __future__ import annotations

from collections.abc import Iterable

import pytest
import requests

from src.ingest import ccct


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


def _success_xml() -> bytes:
    return (
        b"<?xml version='1.0' encoding='UTF-8'?>"
        b"<response><header><resultCode>00</resultCode></header>"
        b"<body><items /></body></response>"
    )


def test_query_requires_credential_before_network(monkeypatch) -> None:
    monkeypatch.setattr(ccct.settings, "data_go_kr_service_key", "")
    monkeypatch.setattr(
        ccct._session,
        "get",
        lambda *_args, **_kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ccct.CcctApiError, match="CCCT_API_CREDENTIAL_UNAVAILABLE"):
        ccct._query("0303540000", "2")


def test_query_uses_no_redirect_bounded_stream_and_safe_result(monkeypatch) -> None:
    response = _Response(chunks=(_success_xml(),))
    observed: dict[str, object] = {}
    monkeypatch.setattr(ccct.settings, "data_go_kr_service_key", "approved-test-key")

    def fake_get(_url, **kwargs):
        observed.update(kwargs)
        return response

    monkeypatch.setattr(ccct._session, "get", fake_get)

    assert ccct._query("0303540000", "2") == []
    assert observed["allow_redirects"] is False
    assert observed["stream"] is True
    assert response.closed


def test_query_rejects_oversized_response(monkeypatch) -> None:
    response = _Response(chunks=(b"x" * (ccct.MAX_RESPONSE_BYTES + 1),))
    monkeypatch.setattr(ccct.settings, "data_go_kr_service_key", "approved-test-key")
    monkeypatch.setattr(ccct._session, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(ccct.CcctApiError, match="CCCT_API_RESPONSE_TOO_LARGE"):
        ccct._query("0303540000", "2")

    assert response.closed


def test_query_transport_error_never_echoes_key(monkeypatch) -> None:
    secret = "approved-test-key"
    monkeypatch.setattr(ccct.settings, "data_go_kr_service_key", secret)
    monkeypatch.setattr(ccct.time, "sleep", lambda _seconds: None)

    def fail(*_args, **_kwargs):
        raise requests.ConnectionError(f"url?serviceKey={secret}")

    monkeypatch.setattr(ccct._session, "get", fail)

    with pytest.raises(ccct.CcctApiError) as captured:
        ccct._query("0303540000", "2", retries=1)

    assert captured.value.code == "CCCT_API_TRANSPORT_FAILURE"
    assert secret not in str(captured.value)


def test_quota_response_is_safe_and_non_retrying(monkeypatch) -> None:
    response = _Response(status=429)
    monkeypatch.setattr(ccct.settings, "data_go_kr_service_key", "approved-test-key")
    monkeypatch.setattr(ccct._session, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(ccct.QuotaExceeded, match="CCCT_API_QUOTA_EXCEEDED"):
        ccct._query("0303540000", "2")

    assert response.closed
