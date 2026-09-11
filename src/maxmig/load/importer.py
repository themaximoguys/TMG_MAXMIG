"""Create-or-update load pipeline — the write-side counterpart to `maxmig.api.oslc`.

For each row: look up whether a record already exists by its declared natural
key (an object's `key_fields`, ANDed via `oslc.where`) and either PATCH it
(update) or POST a new one (create). OSLC only — this tool has no verified
write path for REST MBO, so import always targets an object's `oslc_resource`
regardless of its `access.prefer` for reads.

**Dry-run by default.** `execute=False` (the default throughout this module
and the CLI) only performs the natural-key lookup and reports what it WOULD
do — no POST is ever sent. This mirrors the project's "ask before destructive
actions" posture: unlike `export`, which is always read-only, an import can
create or modify real records in a live Maximo instance.

**Known open risk, not resolved here:** every CSV value arrives as a string
(that's what `write_csv`/`csv.DictReader` produce) and is sent to Maximo as a
JSON string value. Whether Maximo's OSLC write path coerces a string like
"3.0" or "False" into the attribute's real declared type (NUMBER, BOOLEAN) or
rejects it has not been verified against a live `--execute` run as of this
writing — only dry-run (read-only) paths have been exercised live. Treat the
first real `--execute` run on any given object as a validation of that, not
an assumption already settled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

import requests

from maxmig.api.filter import quote_where_value
from maxmig.api.http import get_json, join_url

Action = Literal["would_create", "would_update", "created", "updated", "failed"]


class MissingKeyFieldError(ValueError):
    """Raised when a CSV row is missing a value for one of the object's key fields."""


@dataclass(frozen=True)
class ImportRowResult:
    key: dict[str, str]
    action: Action
    href: str | None = None
    error: str | None = None


def natural_key_where(row: dict[str, str], key_fields: list[str]) -> str:
    """Build the `oslc.where` clause that identifies `row` by its natural key.

    Raises MissingKeyFieldError if any key field is blank/absent in the row —
    querying with an empty key value could match the wrong record (or none),
    silently, rather than failing loudly.
    """
    missing = [field for field in key_fields if not row.get(field)]
    if missing:
        raise MissingKeyFieldError(f"row is missing a value for key field(s): {', '.join(missing)}")
    return " and ".join(f"{field}={quote_where_value(row[field])}" for field in key_fields)


def _non_empty_body(row: dict[str, str], exclude: set[str] = frozenset()) -> dict[str, str]:
    """Fields with a real value, excluding `exclude` — an empty CSV cell means
    "no value was exported for this field", not "set it to blank", so it's
    omitted from the write body rather than sent as an explicit empty string."""
    return {k: v for k, v in row.items() if v != "" and k not in exclude}


def find_existing_href(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    where: str,
    timeout: int = 60,
) -> str | None:
    """Look up whether a record matching `where` already exists. Returns its
    href, or None. Uses `get_json` (retryable) — a lookup GET is always safe
    to retry, unlike the writes below."""
    url = join_url(base_url, f"api/os/{resource}")
    data = get_json(session, url, headers, params={"lean": 1, "oslc.pageSize": 1, "oslc.where": where}, timeout=timeout)
    members = data.get("member", [])
    return members[0].get("href") if members else None


def create_record(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    body: dict[str, Any],
    timeout: int = 60,
) -> str:
    """POST a new record. Returns its href. Deliberately NOT retried here —
    see the module docstring's create/retry note in `import_row`: a blind
    retry on a create risks a duplicate if the first attempt actually
    succeeded server-side and only the response was lost."""
    url = join_url(base_url, f"api/os/{resource}")
    response = session.post(url, json=body, headers=headers, timeout=timeout)
    response.raise_for_status()
    href = response.headers.get("Location")
    if not href and response.content:
        href = response.json().get("href")
    if not href:
        raise RuntimeError(f"create against {resource!r} returned {response.status_code} with no href/Location header")
    return href


def update_record(
    session: requests.Session,
    headers: dict[str, str],
    href: str,
    body: dict[str, Any],
    timeout: int = 60,
) -> None:
    """PATCH an existing record at `href` via POST + x-method-override (Maximo's
    documented mechanism — a bare HTTP PATCH isn't accepted by every gateway)."""
    patch_headers = {**headers, "x-method-override": "PATCH", "patchtype": "MERGE"}
    response = session.post(href, json=body, headers=patch_headers, timeout=timeout)
    response.raise_for_status()


def import_row(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    row: dict[str, str],
    key_fields: list[str],
    execute: bool,
    find_href: Callable[..., str | None] = find_existing_href,
    create: Callable[..., str] = create_record,
    update: Callable[..., None] = update_record,
) -> ImportRowResult:
    """Create-or-update one row. `execute=False` (the default) only performs
    the lookup and reports `would_create`/`would_update` — it never writes."""
    try:
        where = natural_key_where(row, key_fields)
    except MissingKeyFieldError as exc:
        return ImportRowResult(key={f: row.get(f, "") for f in key_fields}, action="failed", error=str(exc))

    key = {field: row[field] for field in key_fields}

    try:
        href = find_href(session, base_url, headers, resource, where)
    except requests.exceptions.HTTPError as exc:
        return ImportRowResult(key=key, action="failed", error=str(exc))

    if not execute:
        return ImportRowResult(key=key, action="would_update" if href else "would_create", href=href)

    if href:
        body = _non_empty_body(row, exclude=set(key_fields))
        try:
            update(session, headers, href, body)
        except requests.exceptions.HTTPError as exc:
            return ImportRowResult(key=key, action="failed", href=href, error=str(exc))
        return ImportRowResult(key=key, action="updated", href=href)

    body = _non_empty_body(row)
    try:
        new_href = create(session, base_url, headers, resource, body)
    except requests.exceptions.HTTPError as exc:
        # The create may have actually landed server-side even though this
        # request errored (timeout, 5xx) -- re-check by natural key rather
        # than assume failure, so re-running an import never double-creates.
        try:
            recheck_href = find_href(session, base_url, headers, resource, where)
        except requests.exceptions.HTTPError:
            recheck_href = None
        if recheck_href:
            return ImportRowResult(key=key, action="created", href=recheck_href)
        return ImportRowResult(key=key, action="failed", error=str(exc))
    return ImportRowResult(key=key, action="created", href=new_href)
