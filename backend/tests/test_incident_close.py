from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.incident_close import close_open_incidents


class FakeOpener:
    def __init__(self, incidents):
        self.incidents = incidents
        self.requests = []

    @contextmanager
    def _response(self, status, body=b""):
        class Response:
            pass

        response = Response()
        response.status = status
        response.read = lambda: body
        yield response

    def open(self, request, timeout=None):
        url = request.full_url
        self.requests.append((request.get_method(), url))
        if url.endswith("/api/v1/login"):
            assert json.loads(request.data) == {"username": "u", "password": "p"}
            return self._response(200)
        if url.endswith("/api/v1/incidents"):
            return self._response(200, json.dumps(self.incidents).encode())
        if "/close" in url:
            return self._response(200)
        raise AssertionError(f"unexpected url: {url}")


def test_closes_only_non_closed_incidents_and_reports_them():
    opener = FakeOpener(
        [
            {"incident_id": "a1", "status": "active", "title": "pg session"},
            {"incident_id": "b2", "status": "closed", "title": "old"},
            {"incident_id": "c3", "status": "waiting", "title": "net"},
        ]
    )
    closed = close_open_incidents(
        query_url="http://q", observer_url="http://o",
        username="u", password="p", opener_factory=lambda: opener,
    )
    assert [item["id"] for item in closed] == ["a1", "c3"]
    assert ("POST", "http://o/api/v1/incidents/a1/close") in opener.requests
    assert all("/incidents/b2/close" not in url for _method, url in opener.requests)


def test_missing_credentials_raise(monkeypatch):
    monkeypatch.delenv("LUCIDA_LOGIN_USER", raising=False)
    monkeypatch.delenv("LUCIDA_LOGIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        close_open_incidents(query_url="http://q", observer_url="http://o")
