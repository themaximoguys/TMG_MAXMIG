"""REST MBO access — generic pattern for any Maximo table.

GET {base_url}/rest/mbo/{table}?_format=json&_maxItems=N&_rsStart=M&{FIELD}={VALUE}
Response: {TABLE}MboSet.{TABLE}[].Attributes.{FIELD}.content
No auto-pagination — callers must page manually via _rsStart/_maxItems.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

import requests

from maxmig.api.http import get_json, join_url

_EMPTY_ATTRS: dict[str, Any] = {}


def get_attr(item: dict[str, Any], key: str) -> str:
    return item.get("Attributes", _EMPTY_ATTRS).get(key, _EMPTY_ATTRS).get("content", "")


def flatten_rest_mbo_row(item: dict[str, Any], fields_upper: list[tuple[str, str]]) -> dict[str, str]:
    """`fields_upper` is `[(field, field.upper()), ...]`, precomputed once per table fetch."""
    return {field: get_attr(item, field_upper) for field, field_upper in fields_upper}


def fetch_rest_mbo_page(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    table: str,
    rs_start: int,
    max_items: int,
    filters: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[list[dict[str, Any]], int, int]:
    """Fetch one REST MBO page. Returns (raw_rows, rs_count, rs_total)."""
    url = join_url(base_url, f"rest/mbo/{table}")
    params: dict[str, Any] = {
        "_format": "json",
        "_maxItems": max_items,
        "_rsStart": rs_start,
        **(filters or {}),
    }
    data = get_json(session, url, headers, params=params, timeout=timeout)
    mbo_set = data.get(f"{table.upper()}MboSet", {})
    rows = mbo_set.get(table.upper(), [])
    rs_count = mbo_set.get("rsCount", len(rows))
    rs_total = mbo_set.get("rsTotal", rs_count)
    return rows, rs_count, rs_total


def iter_rest_mbo(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    table: str,
    fields: list[str],
    filters: dict[str, str] | None = None,
    page_size: int = 250,
    start_rs_start: int = 0,
    page_fetcher: Callable[..., tuple[list[dict[str, Any]], int, int]] = fetch_rest_mbo_page,
) -> Iterator[dict[str, str]]:
    """Yield flattened rows across all REST MBO pages for `table`."""
    fields_upper = [(field, field.upper()) for field in fields]  # computed once, not per row
    rs_start = start_rs_start
    while True:
        rows, rs_count, rs_total = page_fetcher(
            session, base_url, headers, table, rs_start, page_size, filters
        )
        for raw in rows:
            yield flatten_rest_mbo_row(raw, fields_upper)
        if rs_count < page_size or rs_start + rs_count >= rs_total:
            break
        rs_start += rs_count
