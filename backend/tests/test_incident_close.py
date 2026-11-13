from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from contextlib import contextmanager

import pytest

from app.incident_close import IncidentCloseError, close_open_incidents, open_incident_count


@pytest.fixture(autouse=True)
def _no_endpoint_env(monkeypatch):
    for name in (
        "LUCIDA_QUERY_URL",
        "LUCIDA_OBSERVER_URL",
        "LUCIDA_INCIDENTS_URL",
        "LUCIDA_REQUEST_ORIGIN",
    ):
        monkeypatch.delenv(name, raising=False)


class FakeOpener:
    """Login plus an incidents API with the server rules that matter here.

    ``pages`` is the list of pages the API serves for ``status=active`` (the
    runner must follow ``nextCursor``). ``unfiltered`` is what a request
    without that filter would see. A close is refused with 403 "forbidden
    origin" unless Origin is in ``allowed_origins`` (the lucida cookie-write
    rule); ids in ``refuse`` are refused with 403 regardless."""

    def __init__(
        self,
        incidents=None,
        *,
        pages=None,
        collection="http://o/api/v1/incidents",
        allowed_origins=("http://q",),
        refuse=(),
    ):
        if pages is None:
            pages = [incidents or []]
        self.pages = pages
        self.collection = collection
        self.allowed_origins = set(allowed_origins)
        self.refuse = set(refuse)
        self.requests = []
        self.headers = []

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
        self.headers.append({key.lower(): value for key, value in request.header_items()})
        parts = urllib.parse.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if parts.path.endswith("/api/v1/login"):
            assert json.loads(request.data) == {"username": "u", "password": "p"}
            return self._response(200)
        if base == self.collection and request.get_method() == "GET":
            query = urllib.parse.parse_qs(parts.query)
            assert query.get("status") == ["active"], url
            cursor = query.get("cursor", ["0"])[0]
            index = int(cursor)
            items = self.pages[index]
            next_cursor = str(index + 1) if index + 1 < len(self.pages) else ""
            document = {"items": items, "total": 0, "nextCursor": next_cursor}
            return self._response(200, json.dumps(document).encode())
        if base.startswith(self.collection + "/") and base.endswith("/close"):
            assert request.get_method() == "POST"
            incident_id = base[len(self.collection) + 1 : -len("/close")]
            origin = request.get_header("Origin")
            if origin not in self.allowed_origins:
                raise urllib.error.HTTPError(
                    url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"forbidden origin"}')
                )
            if incident_id in self.refuse:
                raise urllib.error.HTTPError(
                    url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"permission denied"}')
                )
            return self._response(200)
        raise AssertionError(f"unexpected url: {url}")

    def closes(self):
        return [url for method, url in self.requests if method == "POST" and "/close" in url]


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


def test_close_sends_the_gateway_origin_and_referer_a_cookie_write_needs():
    """11-13: a cookie-only POST was refused with 403 "forbidden origin" and
    the refusal was swallowed. The fake refuses exactly that request."""
    opener = FakeOpener([{"id": "a1", "status": "active"}])
    close_open_incidents(
        query_url="http://q", observer_url="http://o",
        username="u", password="p", opener_factory=lambda: opener,
    )
    post = [
        headers
        for (method, url), headers in zip(opener.requests, opener.headers)
        if method == "POST" and url.endswith("/close")
    ]
    assert post == [
        {"content-type": "application/json", "origin": "http://q", "referer": "http://q/"}
    ]


def test_default_route_is_the_web_gateway_under_the_login_base(monkeypatch):
    monkeypatch.setenv("LUCIDA_QUERY_URL", "http://gw:13080/")
    opener = FakeOpener(
        [{"id": "a1", "status": "active"}],
        collection="http://gw:13080/api/v1/ai/incidents",
        allowed_origins=("http://gw:13080",),
    )
    closed = close_open_incidents(username="u", password="p", opener_factory=lambda: opener)
    assert [item["id"] for item in closed] == ["a1"]
    assert opener.requests[0] == ("POST", "http://gw:13080/api/v1/login")
    assert opener.requests[1][1].startswith("http://gw:13080/api/v1/ai/incidents?")
    assert opener.closes() == ["http://gw:13080/api/v1/ai/incidents/a1/close"]


def test_empty_observer_env_falls_back_to_the_gateway(monkeypatch):
    """docker-compose passes LUCIDA_OBSERVER_URL through as an empty value."""
    monkeypatch.setenv("LUCIDA_QUERY_URL", "http://gw:13080")
    monkeypatch.setenv("LUCIDA_OBSERVER_URL", "")
    monkeypatch.setenv("LUCIDA_INCIDENTS_URL", "")
    opener = FakeOpener([], collection="http://gw:13080/api/v1/ai/incidents")
    assert open_incident_count(username="u", password="p", opener_factory=lambda: opener) == 0


def test_legacy_observer_env_and_explicit_incidents_url(monkeypatch):
    monkeypatch.setenv("LUCIDA_QUERY_URL", "http://q")
    monkeypatch.setenv("LUCIDA_OBSERVER_URL", "http://obs:18087/")
    legacy = FakeOpener([{"id": "a1", "status": "active"}],
                        collection="http://obs:18087/api/v1/incidents")
    assert open_incident_count(username="u", password="p", opener_factory=lambda: legacy) == 1

    monkeypatch.setenv("LUCIDA_INCIDENTS_URL", "http://other/x/incidents/")
    explicit = FakeOpener([{"id": "a1", "status": "active"}],
                          collection="http://other/x/incidents")
    close_open_incidents(username="u", password="p", opener_factory=lambda: explicit)
    assert explicit.closes() == ["http://other/x/incidents/a1/close"]


def test_request_origin_override(monkeypatch):
    monkeypatch.setenv("LUCIDA_REQUEST_ORIGIN", "https://portal.example:8443")
    opener = FakeOpener([{"id": "a1", "status": "active"}],
                        allowed_origins=("https://portal.example:8443",))
    close_open_incidents(query_url="http://q", observer_url="http://o",
                         username="u", password="p", opener_factory=lambda: opener)
    assert opener.closes() == ["http://o/api/v1/incidents/a1/close"]
    assert opener.headers[-1]["referer"] == "https://portal.example:8443/"


def test_open_incidents_on_a_later_page_are_counted_and_closed():
    """The unfiltered first page is the newest 200 of any state, so an older
    active incident used to be invisible to the gate and to the sweep."""
    opener = FakeOpener(pages=[
        [{"id": "n1", "status": "active"}],
        [{"id": "o1", "status": "active"}, {"id": "o2", "status": "active"}],
    ])
    count = open_incident_count(query_url="http://q", observer_url="http://o",
                                username="u", password="p", opener_factory=lambda: opener)
    assert count == 3
    closed = close_open_incidents(query_url="http://q", observer_url="http://o",
                                  username="u", password="p", opener_factory=lambda: opener)
    assert [item["id"] for item in closed] == ["n1", "o1", "o2"]


def test_a_cursor_that_does_not_advance_fails_instead_of_counting_short():
    class Stuck(FakeOpener):
        def open(self, request, timeout=None):
            if request.get_method() == "GET":
                self.requests.append(("GET", request.full_url))

                @contextmanager
                def page():
                    class Response:
                        status = 200

                        @staticmethod
                        def read():
                            return json.dumps({"items": [], "nextCursor": "same"}).encode()

                    yield Response()

                return page()
            return super().open(request, timeout)

    opener = Stuck([])
    with pytest.raises(RuntimeError, match="did not advance"):
        open_incident_count(query_url="http://q", observer_url="http://o",
                            username="u", password="p", opener_factory=lambda: opener)


def test_superseded_and_resolved_are_not_open():
    opener = FakeOpener([
        {"id": "a1", "status": "active"},
        {"id": "s1", "status": "superseded"},
        {"id": "r1", "status": "resolved"},
    ])
    assert open_incident_count(query_url="http://q", observer_url="http://o",
                               username="u", password="p", opener_factory=lambda: opener) == 1


def test_a_refused_close_does_not_stop_the_rest_and_is_reported():
    opener = FakeOpener(
        [
            {"id": "a1", "status": "active", "title": "first"},
            {"id": "b2", "status": "active", "title": "refused"},
            {"id": "c3", "status": "active", "title": "third"},
        ],
        refuse=("b2",),
    )
    with pytest.raises(IncidentCloseError) as raised:
        close_open_incidents(query_url="http://q", observer_url="http://o",
                             username="u", password="p", opener_factory=lambda: opener)
    error = raised.value
    assert [item["id"] for item in error.closed] == ["a1", "c3"]
    assert [item["id"] for item in error.failed] == ["b2"]
    assert error.failed[0]["error"].startswith("HTTP 403")
    assert "permission denied" in error.failed[0]["error"]
    assert "1 of 3" in str(error)
    assert len(opener.closes()) == 3


def test_a_cookie_only_close_is_a_reported_failure_not_a_silent_one(monkeypatch):
    """Origin outside the allowed set reproduces the 11-13 refusal."""
    monkeypatch.setenv("LUCIDA_REQUEST_ORIGIN", "http://not-allowed")
    opener = FakeOpener([{"id": "a1", "status": "active"}])
    with pytest.raises(IncidentCloseError, match="forbidden origin"):
        close_open_incidents(query_url="http://q", observer_url="http://o",
                             username="u", password="p", opener_factory=lambda: opener)


def test_missing_credentials_raise(monkeypatch):
    monkeypatch.delenv("LUCIDA_LOGIN_USER", raising=False)
    monkeypatch.delenv("LUCIDA_LOGIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="not configured"):
        close_open_incidents(query_url="http://q", observer_url="http://o")


def test_open_incident_count_counts_only_non_closed():
    opener = FakeOpener(
        [
            {"incident_id": "a1", "status": "active"},
            {"incident_id": "b2", "status": "closed"},
            {"incident_id": "c3", "status": "waiting"},
        ]
    )
    count = open_incident_count(
        query_url="http://q", observer_url="http://o",
        username="u", password="p", opener_factory=lambda: opener,
    )
    assert count == 2
    assert all("/close" not in url for _method, url in opener.requests)
