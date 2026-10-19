"""MaximoClient — auth + the REST-MBO/OSLC dual-path fetch with fallback.

Duck-types its `obj_def` argument (an `ObjectDef` from maxmig.model.schema) rather
than importing that module, so this layer has zero dependency on the model layer
at this phase — it only needs `.access.{prefer,oslc_resource,rest_mbo_table}`,
`.fields`, `.key_fields`, and `.where`.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Callable, Iterator

import requests

from maxmig.api.filter import Filter, UnsupportedFilterError, to_oslc_where, to_rest_mbo_params
from maxmig.api.oslc import SkippedRow, build_oslc_select, iter_oslc, iter_oslc_pages, iter_oslc_skip_errors
from maxmig.api.rest_mbo import iter_rest_mbo, iter_rest_mbo_pages
from maxmig.config import EnvConfig

if TYPE_CHECKING:
    from maxmig.model.schema import ObjectDef

# Which access-def attribute holds each API's resource identifier — the single
# source of truth _api_order maps over, instead of a parallel if/elif chain.
_RESOURCE_ATTR = {"oslc": "oslc_resource", "rest_mbo": "rest_mbo_table"}
_ALTERNATE_API = {"oslc": "rest_mbo", "rest_mbo": "oslc"}


def _rest_mbo_fields(obj_def: "ObjectDef") -> list[str] | None:
    """Columns REST MBO should flatten: key fields, standard fields, then custom
    ones — or None (every attribute) under `capture_all_attributes`."""
    if getattr(obj_def, "capture_all_attributes", False):
        return None
    custom = getattr(obj_def, "custom_fields", None) or []
    ordered = list(obj_def.key_fields) + list(obj_def.fields) + list(custom)
    return list(dict.fromkeys(ordered))


class MaximoAuthError(RuntimeError):
    """Raised when an EnvConfig has neither an api_key nor a username/password pair."""


def build_auth_header(username: str, password: str) -> str:
    return base64.b64encode(f"{username}:{password}".encode()).decode()


class MaximoClient:
    def __init__(
        self,
        env: EnvConfig,
        session: requests.Session | None = None,
        page_size: int = 250,
    ):
        self.env = env
        self.session = session or requests.Session()
        self.page_size = page_size
        # apikey preferred over maxauth when both are set — some MAS instances
        # are SSO/OIDC-fronted and don't accept a bare maxauth header at all,
        # making a Maximo API key the only headless auth path available.
        if env.api_key:
            self._auth_header_name = "apikey"
            self._auth_header_value = env.api_key
        elif env.username and env.password:
            self._auth_header_name = "maxauth"
            self._auth_header_value = build_auth_header(env.username, env.password)
        else:
            raise MaximoAuthError(
                f"{env.prefix}: no api_key and no username/password configured — set either "
                f"{env.prefix}_MANAGE_APIKEY or both {env.prefix}_MANAGE_USERNAME/_PASSWORD"
            )

    @property
    def headers(self) -> dict[str, str]:
        return {self._auth_header_name: self._auth_header_value, "Accept": "application/json"}

    def _api_order(self, obj_def: "ObjectDef", filt: Filter | None) -> list[str]:
        """Preferred API first, then whichever alternate can actually express
        the current filter.

        Eligibility is decided by *asking* `to_rest_mbo_params` (the one place
        that knows what REST MBO can express) rather than re-deriving its
        limits here — that keeps this fallback-safety check in lockstep with
        filter.py automatically as its capabilities change, instead of two
        independently-maintained pictures of "what REST MBO can express".
        """
        access = obj_def.access
        order = [access.prefer]
        alternate = _ALTERNATE_API[access.prefer]
        alternate_configured = bool(getattr(access, _RESOURCE_ATTR[alternate]))
        if alternate_configured and self._can_express(alternate, obj_def, filt):
            order.append(alternate)
        return order

    def _can_express(self, api: str, obj_def: "ObjectDef", filt: Filter | None) -> bool:
        if api != "rest_mbo":
            return True  # OSLC can express anything a Filter/where can produce
        try:
            to_rest_mbo_params(obj_def.where, filt)
        except UnsupportedFilterError:
            return False
        return True

    def _fetch_via(
        self,
        api: str,
        obj_def: "ObjectDef",
        filt: Filter | None,
        skip_errors: bool = False,
        on_skip: Callable[[SkippedRow], None] | None = None,
    ) -> Iterator[dict]:
        access = obj_def.access
        if api == "oslc":
            if skip_errors:
                yield from iter_oslc_skip_errors(
                    self.session,
                    self.env.manage_url,
                    self.headers,
                    access.oslc_resource,
                    page_size=self.page_size,
                    where=to_oslc_where(obj_def.where, filt),
                    select=build_oslc_select(obj_def.children),
                    on_skip=on_skip,
                )
            else:
                yield from iter_oslc(
                    self.session,
                    self.env.manage_url,
                    self.headers,
                    access.oslc_resource,
                    page_size=self.page_size,
                    where=to_oslc_where(obj_def.where, filt),
                    select=build_oslc_select(obj_def.children),
                )
        else:
            if skip_errors:
                raise ValueError("skip_errors is only supported for the OSLC access path")
            yield from iter_rest_mbo(
                self.session,
                self.env.manage_url,
                self.headers,
                access.rest_mbo_table,
                _rest_mbo_fields(obj_def),
                filters=to_rest_mbo_params(obj_def.where, filt),
                page_size=self.page_size,
            )

    def fetch_object(
        self,
        obj_def: "ObjectDef",
        filter: Filter | None = None,
        skip_errors: bool = False,
        on_skip: Callable[[SkippedRow], None] | None = None,
    ) -> Iterator[dict]:
        """Yield rows for `obj_def`, using access.prefer with one automatic fallback.

        `filter` narrows the result (site scoping, field conditions, or a raw
        OSLC where override) — see maxmig.api.filter for precedence rules.

        `skip_errors` (OSLC only) skips-and-logs individual records that fail
        with a known-recoverable Maximo error (e.g. the rotating-item
        BMXAA1825E bug on MAXDEMO's INVENTORY table) via `on_skip`, instead of
        aborting the whole export — see maxmig.api.oslc.iter_oslc_skip_errors.

        Only falls back if the *first page* of the preferred API errors (non-2xx) —
        this avoids yielding a partial result from one API and then silently
        restarting from the other, which would duplicate already-yielded rows.
        """
        api_order = self._api_order(obj_def, filter)
        last_exc: requests.exceptions.HTTPError | None = None
        for api in api_order:
            gen = self._fetch_via(api, obj_def, filter, skip_errors=skip_errors, on_skip=on_skip)
            try:
                first_row = next(gen)
            except StopIteration:
                return  # empty result set — valid, nothing more to try
            except requests.exceptions.HTTPError as exc:
                last_exc = exc
                continue
            yield first_row
            yield from gen
            return
        assert last_exc is not None
        raise last_exc

    def _pages_via(
        self,
        api: str,
        obj_def: "ObjectDef",
        filt: Filter | None,
        cursor: int | None,
        skip_errors: bool,
        on_skip: Callable[[SkippedRow], None] | None,
    ) -> Iterator[tuple[int | None, list[dict]]]:
        access = obj_def.access
        if api == "oslc":
            for pageno, rows, reached_end in iter_oslc_pages(
                self.session,
                self.env.manage_url,
                self.headers,
                access.oslc_resource,
                page_size=self.page_size,
                where=to_oslc_where(obj_def.where, filt),
                select=build_oslc_select(obj_def.children),
                start_page=cursor or 1,
                skip_errors=skip_errors,
                on_skip=on_skip,
            ):
                yield (None if reached_end else pageno + 1), rows
        else:
            if skip_errors:
                raise ValueError("skip_errors is only supported for the OSLC access path")
            yield from iter_rest_mbo_pages(
                self.session,
                self.env.manage_url,
                self.headers,
                access.rest_mbo_table,
                _rest_mbo_fields(obj_def),
                filters=to_rest_mbo_params(obj_def.where, filt),
                page_size=self.page_size,
                start_rs_start=cursor or 0,
            )

    def fetch_pages(
        self,
        obj_def: "ObjectDef",
        filter: Filter | None = None,
        skip_errors: bool = False,
        on_skip: Callable[[SkippedRow], None] | None = None,
        api: str | None = None,
        cursor: int | None = None,
    ) -> Iterator[tuple[str, int | None, list[dict]]]:
        """Like `fetch_object`, a page at a time, for checkpointed exports:
        yields `(api, next_cursor, rows)` where `next_cursor` is the OSLC
        `pageno` or REST MBO `_rsStart` to resume from (None after the last
        page). Pages are addressed explicitly, never through nextPage links,
        so a later process can pick up at `cursor`.

        With `api` given (a resume), that API is used with no fallback —
        switching APIs mid-export would page a different ordering. Otherwise
        the same first-page-only fallback as `fetch_object` applies.
        """
        api_order = [api] if api else self._api_order(obj_def, filter)
        last_exc: requests.exceptions.HTTPError | None = None
        for candidate in api_order:
            gen = self._pages_via(candidate, obj_def, filter, cursor, skip_errors, on_skip)
            try:
                next_cursor, rows = next(gen)
            except StopIteration:
                return
            except requests.exceptions.HTTPError as exc:
                last_exc = exc
                continue
            yield candidate, next_cursor, rows
            for next_cursor, rows in gen:
                yield candidate, next_cursor, rows
            return
        assert last_exc is not None
        raise last_exc

