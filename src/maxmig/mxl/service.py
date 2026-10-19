"""REST service — port of MxLoader's MxMifService.cls (REST branch).

`sync` is SyncRest + DoHttpCall: one POST per batch to the object structure
with `x-method-override: BULK`, a JSON array body whose records each carry
`_action`, and a `properties` header listing the `[ID]` attributes to return.
Maximo answers with one `{_responsemeta: {status}, _responsedata: {...}}`
element per record, so a bad record fails alone while the rest of the batch
commits — the reason MxLoader recommends REST over the OS/XML service.

`query` is QueryRest: GET with `oslc.select` built from the header,
`oslc.where`, `oslc.orderBy` and any extra OSLC directives, following
`responseInfo.nextPage`.

Writes are never retried automatically (a lost response to an `Add` must not
become a duplicate); reads go through the shared retry policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import parse_qsl

import requests

from maxmig.api.http import get_json, join_url
from maxmig.api.oslc import extract_next_page

OK_STATUSES = frozenset({"200", "201", "204"})
_REST_PARAMS = {"lean": 1, "ignorecollectionref": 1, "ignorekeyref": 1, "ignorers": 1}


@dataclass(frozen=True)
class RecordResult:
    """Outcome of one record within a BULK request."""

    status: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES

    @property
    def created(self) -> bool:
        return self.status == "201"


def _ci(obj: Any, key: str) -> Any:
    """Case-insensitive dict lookup — lean responses are lowercase, MxLoader's
    parser upper-cases everything; accept either."""
    if not isinstance(obj, dict):
        return None
    for k, v in obj.items():
        if str(k).lower() == key:
            return v
    return None


def error_message(body: Any) -> str:
    """Pull the human-readable message out of a Maximo error body
    (`Error.message`, `Error.extendedError.message`, or `oslc:Error`)."""
    error = _ci(body, "error") or _ci(body, "oslc:error")
    if isinstance(error, dict):
        message = _ci(error, "message") or _ci(error, "oslc:message")
        if message:
            return str(message)
        extended = _ci(error, "extendederror")
        if isinstance(extended, dict) and _ci(extended, "message"):
            return str(_ci(extended, "message"))
    return ""


def parse_bulk_response(body: Any, expected: int) -> list[RecordResult]:
    items = body if isinstance(body, list) else _ci(body, "member") or []
    results: list[RecordResult] = []
    for item in items[:expected]:
        meta = _ci(item, "_responsemeta") or {}
        status = str(_ci(meta, "status") or "")
        data = _ci(item, "_responsedata") or {}
        if status in OK_STATUSES:
            ids = {str(k).lower(): v for k, v in data.items() if not isinstance(v, (dict, list))} if isinstance(data, dict) else {}
            results.append(RecordResult(status, "OK", ids))
        else:
            results.append(RecordResult(status or "?", error_message(data) or f"Maximo returned status {status or 'none'}"))
    while len(results) < expected:
        results.append(RecordResult("?", "no result returned for this record in the BULK response"))
    return results


class MxRestService:
    def __init__(self, session: requests.Session, base_url: str, headers: dict[str, str], timeout: int = 300):
        self.session = session
        self.base_url = base_url
        self.headers = headers
        self.timeout = timeout

    def _url(self, object_service: str) -> str:
        return join_url(self.base_url, f"api/os/{object_service.lower()}")

    def sync(
        self, object_service: str, action: str, records: list[dict[str, Any]], id_attrs: list[str] | None = None
    ) -> list[RecordResult]:
        """POST one BULK batch. Transport/whole-request failures are reported
        as a failure of every record in the batch, never raised."""
        if not records:
            return []
        headers = {**self.headers, "x-method-override": "BULK", "Content-Type": "application/json"}
        if id_attrs:
            headers["properties"] = ",".join(a.lower() for a in id_attrs)
        params = {**_REST_PARAMS, "mxlaction": action.lower()}
        try:
            response = self.session.post(
                self._url(object_service), params=params, json=records, headers=headers, timeout=self.timeout
            )
        except requests.exceptions.RequestException as exc:
            return [RecordResult("error", f"request failed: {exc}") for _ in records]
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = None
        if response.status_code >= 400:
            message = error_message(body) or f"HTTP {response.status_code}: {response.text[:500]}"
            return [RecordResult(str(response.status_code), message) for _ in records]
        if body is None:
            # Some gateways answer an all-success BULK with an empty 200/204.
            return [RecordResult(str(response.status_code), "OK") for _ in records]
        return parse_bulk_response(body, len(records))

    def query(
        self,
        object_service: str,
        select: str,
        where: str = "",
        order_by: str = "",
        directives: str = "",
        page_size: int = 500,
        max_rows: int = 0,
    ) -> Iterator[dict[str, Any]]:
        """Yield members across pages; stop after `max_rows` members (0 = all)."""
        params: dict[str, Any] = {
            **_REST_PARAMS,
            "mxlaction": "query",
            "oslc.select": select.lower(),
            "oslc.pageSize": min(page_size, max_rows) if max_rows else page_size,
        }
        if where:
            params["oslc.where"] = where
        if order_by:
            params["oslc.orderBy"] = order_by
        for key, value in parse_qsl(directives or "", keep_blank_values=True):
            params[key] = value

        url: str | None = self._url(object_service)
        yielded = 0
        first = True
        while url:
            data = get_json(self.session, url, self.headers, params=params if first else None, timeout=self.timeout)
            first = False
            for member in data.get("member", []):
                yield member
                yielded += 1
                if max_rows and yielded >= max_rows:
                    return
            url = extract_next_page((data.get("responseInfo") or {}).get("nextPage"))
