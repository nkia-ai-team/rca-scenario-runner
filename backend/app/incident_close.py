"""Close open lucida incidents when a capture window ends.

Queue policy (2026-07-21): every scenario capture ends with a clean slate —
any incident still open at capture end (scenario-caused or organic) would let
the judge merge the next scenario's anomalies into it, poisoning the next
case's expected "new incident" answer. Closing via the operator API is a
realistic human action (trigger=human precedent, F08-H) and happens after the
capture window, so the incident's natural lifecycle inside the window is
untouched.

Fail-open by design: a login or close failure must never fail the capture.
"""
from __future__ import annotations

import json
import os
import urllib.request
from http.cookiejar import CookieJar

DEFAULT_QUERY_URL = "http://192.168.230.119:18080"
DEFAULT_OBSERVER_URL = "http://192.168.230.119:18087"
_TIMEOUT_SEC = 10
_CLOSED_STATES = {"closed", "resolved"}


def close_open_incidents(
    *,
    query_url: str | None = None,
    observer_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
    opener_factory=None,
) -> list[dict[str, str]]:
    """Close every non-closed incident; return [{id, status, title}] handled."""
    query_url = (query_url or os.environ.get("LUCIDA_QUERY_URL") or DEFAULT_QUERY_URL).rstrip("/")
    observer_url = (
        observer_url or os.environ.get("LUCIDA_OBSERVER_URL") or DEFAULT_OBSERVER_URL
    ).rstrip("/")
    username = username or os.environ.get("LUCIDA_LOGIN_USER")
    password = password or os.environ.get("LUCIDA_LOGIN_PASSWORD")
    if not username or not password:
        raise RuntimeError("LUCIDA_LOGIN_USER/LUCIDA_LOGIN_PASSWORD are not configured")

    if opener_factory is None:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    else:
        opener = opener_factory()

    login = urllib.request.Request(
        f"{query_url}/api/v1/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(login, timeout=_TIMEOUT_SEC) as response:
        if response.status != 200:
            raise RuntimeError(f"lucida login failed: HTTP {response.status}")

    with opener.open(
        urllib.request.Request(f"{observer_url}/api/v1/incidents"), timeout=_TIMEOUT_SEC
    ) as response:
        document = json.loads(response.read())
    items = document if isinstance(document, list) else (
        document.get("incidents") or document.get("items") or []
    )

    closed: list[dict[str, str]] = []
    for item in items:
        status = str(item.get("status", ""))
        if status in _CLOSED_STATES:
            continue
        incident_id = str(item.get("incident_id") or item.get("id") or "")
        if not incident_id:
            continue
        close = urllib.request.Request(
            f"{observer_url}/api/v1/incidents/{incident_id}/close", data=b"", method="POST"
        )
        with opener.open(close, timeout=_TIMEOUT_SEC) as response:
            if response.status >= 300:
                raise RuntimeError(f"incident close failed: {incident_id}: HTTP {response.status}")
        closed.append(
            {"id": incident_id, "status": status, "title": str(item.get("title", ""))[:120]}
        )
    return closed
