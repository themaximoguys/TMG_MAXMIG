"""Shared GET-with-retry-and-parse helper used by both api/rest_mbo.py and api/oslc.py.

Both access modes end every request with the same three steps (retry, raise on
final non-2xx, parse JSON) — factored here once so a future change to that
sequence (logging, error-body handling, etc.) has one call site, not several.
"""

from __future__ import annotations

from typing import Any

import requests

from maxmig.retry import call_with_retry


def join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def get_json(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """GET `url` with retry/backoff, raise on a non-2xx final response, return parsed JSON."""
    response = call_with_retry(lambda: session.get(url, headers=headers, params=params, timeout=timeout))
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        # SSO/OIDC-fronted MAS with apikey auth: endpoints outside /api/ (e.g.
        # legacy /rest/mbo/) 302 to an HTML login page that `requests` follows
        # to a 200. Surface that as an HTTPError so callers' API fallback and
        # error handling treat it as a failed request, not a crash.
        where = f" (redirected to {response.url})" if response.history else ""
        raise requests.exceptions.HTTPError(
            f"non-JSON response from {url}{where} — likely an SSO login page; "
            f"this endpoint does not accept the configured auth",
            response=response,
        ) from exc
