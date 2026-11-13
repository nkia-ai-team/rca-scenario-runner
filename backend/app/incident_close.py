"""Close open lucida incidents when a capture window ends.

Queue policy (2026-07-21): every scenario capture ends with a clean slate —
any incident still open at capture end (scenario-caused or organic) would let
the judge merge the next scenario's anomalies into it, poisoning the next
case's expected "new incident" answer. Closing via the operator API is a
realistic human action (trigger=human precedent, F08-H) and happens after the
capture window, so the incident's natural lifecycle inside the window is
untouched.

Endpoints (v4, 2026-11-13). 119 runs the k3s install, where the browser
reaches incidents through the web gateway: ``/api/v1/ai/incidents`` on the
gateway is rewritten to the observer's ``/api/v1/incidents``. The runner goes
the same way by default, so a close is the exact request the screen sends.
Two rules of that path the runner must honour:

- Login and incidents share one origin. The session cookie comes from
  ``{LUCIDA_QUERY_URL}/api/v1/login`` and is sent back to the incidents path.
- A cookie-bearing write must carry an allowed Origin (the AI service and
  query both answer 403 "forbidden origin" otherwise). The observer's close
  is a proxy to query that forwards that Origin, so a cookie-only POST never
  closed anything: the sweep swallowed the 403 and 102 incidents piled up
  from 11-11 until the first v4 recording stopped on them.

Precedence for the incidents collection URL: ``incidents_url`` argument,
``observer_url`` argument (direct observer, ``/api/v1/incidents``), env
``LUCIDA_INCIDENTS_URL``, env ``LUCIDA_OBSERVER_URL`` (direct observer, kept
for the evaluation port), then the gateway path under the login base.

Listing asks for ``status=active`` and follows ``nextCursor``: an unfiltered
first page holds the newest 200 incidents of any state, so an active incident
older than those was invisible to the gate and to the sweep.

Fail-open by design for the capture: a login or close failure must never
fail the capture, but it is no longer silent. ``close_open_incidents`` tries
every open incident and raises :class:`IncidentCloseError` naming each one it
could not close, so callers can record what happened.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

# k3s web gateway on 119: serves login (query) and incidents (observer).
DEFAULT_QUERY_URL = "http://192.168.230.119:13080"
GATEWAY_INCIDENTS_PATH = "/api/v1/ai/incidents"
OBSERVER_INCIDENTS_PATH = "/api/v1/incidents"
_TIMEOUT_SEC = 10
# superseded: replaced by another incident (lucida NKIAAI-818), never open.
_CLOSED_STATES = {"closed", "resolved", "superseded"}
_PAGE_LIMIT = 200
# 50 pages of 200 is far beyond any real backlog; hitting it means the cursor
# does not advance, and a count that silently stops there would read as clean.
_MAX_PAGES = 50
_ERROR_BODY_LIMIT = 200


class IncidentCloseError(RuntimeError):
    """Some open incidents could not be closed. Every one was attempted."""

    def __init__(self, *, closed: list[dict[str, str]], failed: list[dict[str, str]]):
        self.closed = closed
        self.failed = failed
        shown = "; ".join(f"{item['id']}: {item['error']}" for item in failed[:3])
        more = f" (+{len(failed) - 3} more)" if len(failed) > 3 else ""
        super().__init__(
            f"incident close failed for {len(failed)} of {len(failed) + len(closed)}: "
            f"{shown}{more}"
        )


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect. urllib turns a POST answered with 301, 302 or
    303 into a GET of the new location, and a 200 there would count as a
    close that never happened. None of these APIs redirects today (the
    observer proxy maps 3xx to 503), so a redirect is an error to surface:
    urllib then raises HTTPError with the 3xx code."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _Endpoints:
    def __init__(self, *, query_url: str, incidents_url: str, origin: str):
        self.query_url = query_url
        self.incidents_url = incidents_url
        self.origin = origin


def resolve_endpoints(
    *,
    query_url: str | None = None,
    observer_url: str | None = None,
    incidents_url: str | None = None,
) -> _Endpoints:
    """Login base, incidents collection URL, and the Origin writes carry."""
    query = (query_url or os.environ.get("LUCIDA_QUERY_URL") or DEFAULT_QUERY_URL).rstrip("/")
    if incidents_url:
        collection = incidents_url.rstrip("/")
    elif observer_url:
        collection = observer_url.rstrip("/") + OBSERVER_INCIDENTS_PATH
    elif os.environ.get("LUCIDA_INCIDENTS_URL"):
        collection = os.environ["LUCIDA_INCIDENTS_URL"].rstrip("/")
    elif os.environ.get("LUCIDA_OBSERVER_URL"):
        collection = os.environ["LUCIDA_OBSERVER_URL"].rstrip("/") + OBSERVER_INCIDENTS_PATH
    else:
        collection = query + GATEWAY_INCIDENTS_PATH
    origin = os.environ.get("LUCIDA_REQUEST_ORIGIN") or _origin_of(query)
    return _Endpoints(query_url=query, incidents_url=collection, origin=origin.rstrip("/"))


def _origin_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise RuntimeError(f"cannot derive an Origin from {url!r}; set LUCIDA_REQUEST_ORIGIN")
    return f"{parts.scheme}://{parts.netloc}"


def open_incident_count(
    *,
    query_url: str | None = None,
    observer_url: str | None = None,
    incidents_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    opener_factory=None,
) -> int:
    """Count non-closed incidents (R6 preflight `open_incidents` signal)."""
    opener, endpoints = _session(
        query_url=query_url, observer_url=observer_url, incidents_url=incidents_url,
        username=username, password=password, opener_factory=opener_factory,
    )
    return len(_open_incidents(opener, endpoints))


def _session(*, query_url, observer_url, incidents_url, username, password, opener_factory):
    endpoints = resolve_endpoints(
        query_url=query_url, observer_url=observer_url, incidents_url=incidents_url
    )
    username = username or os.environ.get("LUCIDA_LOGIN_USER")
    password = password or os.environ.get("LUCIDA_LOGIN_PASSWORD")
    if not username or not password:
        raise RuntimeError("LUCIDA_LOGIN_USER/LUCIDA_LOGIN_PASSWORD are not configured")
    if opener_factory is None:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()), _RefuseRedirect()
        )
    else:
        opener = opener_factory()
    login = urllib.request.Request(
        f"{endpoints.query_url}/api/v1/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(login, timeout=_TIMEOUT_SEC) as response:
        if response.status != 200:
            raise RuntimeError(f"lucida login failed: HTTP {response.status}")
    return opener, endpoints


def _open_incidents(opener, endpoints: _Endpoints) -> list[dict]:
    """Every non-closed incident, across all pages."""
    found: list[dict] = []
    cursor = ""
    for _page in range(_MAX_PAGES):
        params = {"status": "active", "limit": str(_PAGE_LIMIT)}
        if cursor:
            params["cursor"] = cursor
        url = f"{endpoints.incidents_url}?{urllib.parse.urlencode(params)}"
        with opener.open(urllib.request.Request(url), timeout=_TIMEOUT_SEC) as response:
            document = json.loads(response.read())
        if isinstance(document, list):
            items, next_cursor = document, ""
        elif isinstance(document, dict) and isinstance(
            document.get("items", document.get("incidents")), list
        ):
            items = document.get("items", document.get("incidents"))
            next_cursor = str(document.get("nextCursor") or "")
        else:
            # A 200 that is not an incident list (a wrong LUCIDA_INCIDENTS_URL,
            # a portal page) used to read as zero open incidents: a false clean
            # that would let the gate pass. Refuse it instead.
            keys = sorted(document)[:10] if isinstance(document, dict) else type(document).__name__
            raise RuntimeError(f"incident list response from {url} has no items list: {keys}")
        # The status filter is the server's; keep the client check so an older
        # observer that ignores it still cannot count a closed incident.
        found.extend(
            item for item in items if str(item.get("status", "")) not in _CLOSED_STATES
        )
        if not next_cursor:
            return found
        if next_cursor == cursor:
            raise RuntimeError(f"incident list cursor did not advance: {next_cursor!r}")
        cursor = next_cursor
    raise RuntimeError(f"incident list exceeded {_MAX_PAGES} pages; refusing a partial count")


def close_open_incidents(
    *,
    query_url: str | None = None,
    observer_url: str | None = None,
    incidents_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    opener_factory=None,
) -> list[dict[str, str]]:
    """Close every non-closed incident; return [{id, status, title}] handled.

    Raises :class:`IncidentCloseError` after trying them all when any close
    was refused, carrying both the closed and the failed ones."""
    opener, endpoints = _session(
        query_url=query_url, observer_url=observer_url, incidents_url=incidents_url,
        username=username, password=password, opener_factory=opener_factory,
    )
    closed: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for item in _open_incidents(opener, endpoints):
        status = str(item.get("status", ""))
        incident_id = str(item.get("incident_id") or item.get("id") or "")
        if not incident_id:
            continue
        error = _close_one(opener, endpoints, incident_id)
        record = {"id": incident_id, "status": status, "title": str(item.get("title", ""))[:120]}
        if error is None:
            closed.append(record)
        else:
            failed.append({**record, "error": error})
    if failed:
        raise IncidentCloseError(closed=closed, failed=failed)
    return closed


def _close_one(opener, endpoints: _Endpoints, incident_id: str) -> str | None:
    """POST the close the screen sends; None on success, else the reason."""
    close = urllib.request.Request(
        f"{endpoints.incidents_url}/{urllib.parse.quote(incident_id, safe='')}/close",
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            # Same-origin proof for a cookie-bearing write (see module doc).
            "Origin": endpoints.origin,
            "Referer": endpoints.origin + "/",
        },
        method="POST",
    )
    try:
        with opener.open(close, timeout=_TIMEOUT_SEC) as response:
            if response.status >= 300:
                return f"HTTP {response.status}"
            # Second guard for an opener that does follow redirects (an
            # injected one): an answer from another URL is not this close.
            final_url = getattr(response, "geturl", lambda: close.full_url)()
            if final_url != close.full_url:
                return f"redirected to {final_url}"
    except urllib.error.HTTPError as error:
        body = b""
        try:
            body = error.read() or b""
        except Exception:
            pass
        text = body.decode("utf-8", "replace").strip()[:_ERROR_BODY_LIMIT]
        return f"HTTP {error.code}" + (f" {text}" if text else "")
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    return None
