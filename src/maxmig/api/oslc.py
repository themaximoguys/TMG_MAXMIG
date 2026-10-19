"""OSLC access — generic pattern for any published Object Structure.

GET {base_url}/api/os/{resource}?lean=1&oslc.pageSize=N&oslc.where=...&oslc.select=...

Uses the `/api/os/` prefix rather than the legacy `/oslc/os/` one: on SSO/OIDC-fronted
MAS instances, `/oslc/os/` sits behind the interactive login gate and silently
redirects (302 -> OIDC authorize page) even with a valid apikey header, while
`/api/os/` is the same OSLC object-structure API served outside that gate.
Response: member[] + responseInfo.nextPage — follow nextPage verbatim until absent.
Create=POST; Update/Delete=POST + x-method-override: PATCH/DELETE (write support
lands in the load pipeline, not this foundation phase).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol

import requests

from maxmig.api.http import get_json, join_url


class _RelationLike(Protocol):
    """Structural type for RelationDef — avoids importing the model layer from here."""

    relation_attribute: str
    children: list["_RelationLike"]


def _select_clause_for(relation: _RelationLike) -> str:
    if not relation.children:
        return f"{relation.relation_attribute}{{*}}"
    nested = ",".join(_select_clause_for(child) for child in relation.children)
    return f"{relation.relation_attribute}{{*,{nested}}}"


def build_oslc_select(children: list[_RelationLike]) -> str:
    """Build an oslc.select clause covering the object's own fields plus every
    child relation, recursively.

    Always includes `*` for the object's own fields, even with no children —
    omitting oslc.select entirely (the old behavior) is NOT "return everything,
    same as *": real Maximo instead returns next to nothing (just `href`) when
    no select is given at all. `oslc.select=*` is what actually gets the
    object's own declared fields back.
    """
    if not children:
        return "*"
    return "*," + ",".join(_select_clause_for(child) for child in children)


def fetch_oslc_page(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    page_size: int = 250,
    where: str | None = None,
    select: str | None = None,
    next_page_url: str | None = None,
    pageno: int | None = None,
    timeout: int = 60,
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch one OSLC page. Returns (rows, next_page_url_or_None).

    If `next_page_url` is given, it is followed verbatim (it already encodes
    paging state) rather than re-building query params. Otherwise `pageno`
    (1-based) is sent explicitly when given — used only by the skip-errors
    recovery path in `iter_oslc_skip_errors`, which needs to address a
    specific page/row directly rather than following a server-provided link.
    """
    if next_page_url:
        data = get_json(session, next_page_url, headers, timeout=timeout)
    else:
        url = join_url(base_url, f"api/os/{resource}")
        params: dict[str, Any] = {"lean": 1, "oslc.pageSize": page_size}
        if pageno is not None:
            params["pageno"] = pageno
        if where:
            params["oslc.where"] = where
        if select:
            params["oslc.select"] = select
        data = get_json(session, url, headers, params=params, timeout=timeout)

    rows = data.get("member", [])
    next_page = extract_next_page(data.get("responseInfo", {}).get("nextPage"))
    return rows, next_page


# OSLC response keys that are API plumbing, not attributes of the record.
PLUMBING_KEYS = frozenset({"href", "_rowstamp", "localref"})
PLUMBING_SUFFIXES = ("_collectionref", "_imagelibref", "_localref")


def is_plumbing_key(key: str) -> bool:
    """Whether a key in an OSLC record is plumbing (href, _rowstamp,
    *_collectionref, ...) rather than a real attribute — shared by
    `discover` drafts and `export --all-attributes` so both agree."""
    return key in PLUMBING_KEYS or key.startswith("_") or key.endswith(PLUMBING_SUFFIXES)


def extract_next_page(next_page: Any) -> str | None:
    """Real Maximo (lean=1) nests nextPage as `{"href": "<url>"}`, not a bare
    string — non-lean responses use `{"rdf:resource": "<url>"}` instead. Accept
    a plain string too, for callers that don't request lean mode."""
    if isinstance(next_page, dict):
        return next_page.get("href") or next_page.get("rdf:resource")
    return next_page


def iter_oslc(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    page_size: int = 250,
    where: str | None = None,
    select: str | None = None,
    page_fetcher: Callable[..., tuple[list[dict[str, Any]], str | None]] = fetch_oslc_page,
) -> Iterator[dict[str, Any]]:
    """Yield rows across all OSLC pages for `resource`, following nextPage."""
    next_page_url: str | None = None
    first = True
    while first or next_page_url:
        first = False
        rows, next_page_url = page_fetcher(
            session,
            base_url,
            headers,
            resource,
            page_size=page_size,
            where=where,
            select=select,
            next_page_url=next_page_url,
        )
        yield from rows


# Maximo reason codes that are safe to skip-and-log rather than aborting the
# whole export. Confirmed live against maxdemo's INVENTORY table (2026-09-11):
# certain rotating-item inventory records throw BMXAA1825E on a plain read —
# an interactive "Inventory Usage" transfer business rule firing where it
# shouldn't, not a real client/query error. Extend this set if another
# environment surfaces a different, similarly-spurious GET-time error.
_SKIPPABLE_REASON_CODES = frozenset({"BMXAA1825E"})

# Maximo's own "you've paged past the last record" signal (confirmed live:
# requesting a pageno beyond the table's row count throws this rather than
# returning an empty page) — distinct from a skippable error: it means the
# recovery scan has reached the true end of the table, not a bad record.
_PAGE_NOT_FOUND_REASON_CODE = "BMXAA8732E"


@dataclass(frozen=True)
class SkippedRow:
    """One record a skip-errors export couldn't read, for logging/reporting."""

    resource: str
    pageno: int
    reason_code: str | None
    message: str


def _reason_code(exc: requests.exceptions.HTTPError) -> str | None:
    response = exc.response
    if response is None:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("Error") if isinstance(body, dict) else None
    return error.get("reasonCode") if isinstance(error, dict) else None


def iter_oslc_skip_errors(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    page_size: int = 250,
    where: str | None = None,
    select: str | None = None,
    on_skip: Callable[[SkippedRow], None] | None = None,
    page_fetcher: Callable[..., tuple[list[dict[str, Any]], str | None]] = fetch_oslc_page,
) -> Iterator[dict[str, Any]]:
    """Like `iter_oslc`, but a page that fails with a known-recoverable Maximo
    error (see `_SKIPPABLE_REASON_CODES`) is retried one row at a time so only
    the actually-offending record(s) are skipped — reported via `on_skip` — and
    every other row in that page is still returned, instead of the whole
    export aborting.

    Uses explicit `pageno` pagination throughout (not nextPage-following),
    since a per-row recovery needs to address specific rows directly and must
    stay in lockstep with the full-page cursor afterwards.
    """
    for _, rows, _ in iter_oslc_pages(
        session, base_url, headers, resource, page_size=page_size, where=where, select=select,
        skip_errors=True, on_skip=on_skip, page_fetcher=page_fetcher,
    ):
        yield from rows


def iter_oslc_pages(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    page_size: int = 250,
    where: str | None = None,
    select: str | None = None,
    start_page: int = 1,
    skip_errors: bool = False,
    on_skip: Callable[[SkippedRow], None] | None = None,
    page_fetcher: Callable[..., tuple[list[dict[str, Any]], str | None]] = fetch_oslc_page,
) -> Iterator[tuple[int, list[dict[str, Any]], bool]]:
    """Yield `(pageno, rows, reached_end)` one page at a time, addressing pages
    by explicit `pageno` from `start_page` — so a caller can checkpoint after
    any page and later resume from the next one. With `skip_errors`, a page
    failing with a skippable reason code is recovered row by row (see
    `iter_oslc_skip_errors`); without it, that error is raised."""
    pageno = start_page
    while True:
        try:
            rows, _ = page_fetcher(
                session, base_url, headers, resource,
                page_size=page_size, where=where, select=select, pageno=pageno,
            )
            reached_end = len(rows) < page_size
        except requests.exceptions.HTTPError as exc:
            code = _reason_code(exc)
            if code == _PAGE_NOT_FOUND_REASON_CODE:
                return  # exact multiple of page_size -- the page after the last one
            if not skip_errors or code not in _SKIPPABLE_REASON_CODES:
                raise
            rows, reached_end = _recover_page_row_by_row(
                page_fetcher, session, base_url, headers, resource,
                pageno, page_size, where, select, on_skip,
            )
        yield pageno, rows, reached_end
        if reached_end:
            return
        pageno += 1


def _recover_page_row_by_row(
    page_fetcher: Callable[..., tuple[list[dict[str, Any]], str | None]],
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    resource: str,
    pageno: int,
    page_size: int,
    where: str | None,
    select: str | None,
    on_skip: Callable[[SkippedRow], None] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Re-fetch one failing page's row range one row at a time (page_size=1
    each). Returns (rows, reached_end) — `reached_end` is only True when the
    table itself ran out mid-window, never merely because rows were skipped.
    """
    start = (pageno - 1) * page_size + 1
    rows: list[dict[str, Any]] = []
    reached_end = False
    for row_pageno in range(start, start + page_size):
        try:
            page_rows, _ = page_fetcher(
                session, base_url, headers, resource,
                page_size=1, where=where, select=select, pageno=row_pageno,
            )
        except requests.exceptions.HTTPError as exc:
            code = _reason_code(exc)
            if code == _PAGE_NOT_FOUND_REASON_CODE:
                reached_end = True
                break
            if code not in _SKIPPABLE_REASON_CODES:
                raise
            if on_skip:
                on_skip(SkippedRow(resource=resource, pageno=row_pageno, reason_code=code, message=str(exc)))
            continue
        rows.extend(page_rows)
    return rows, reached_end


_extract_next_page = extract_next_page  # backwards-compatible private alias
