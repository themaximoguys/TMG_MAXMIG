"""A structured query/filter facility for extraction — site scoping, field
conditions, and a raw-where escape hatch — translated to both OSLC's
`oslc.where` boolean-expression syntax and REST MBO's `{FIELD}={VALUE}` params.

Precedence (most specific wins): `raw_where` set -> used verbatim, `site`/
`conditions` ignored entirely and the object's own default `where` (from its
YAML `where:` field) is also ignored, since a raw override means the caller is
taking full manual control of the query. Otherwise the object's default
`where`, `site`, and `conditions` are all AND'd together — narrowing an
already-scoped object (e.g. `storeroom`'s built-in `type="STOREROOM"`) is the
common case, not a replacement.

`raw_where` deliberately mirrors MxLoader's actual filter mechanism (verified
firsthand from its .xlsm/VBA: a single free-text cell passed near-verbatim as
`oslc.where`, with no structured site/field builder at all) — this module adds
`site`/`conditions` as a convenience layer *beyond* what MxLoader offers, not
because MxLoader has one and we were missing it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class UnsupportedFilterError(ValueError):
    """Raised when a filter can't be expressed via the API path it's being sent to."""


@dataclass(frozen=True)
class Filter:
    site: str | None = None
    conditions: dict[str, str] = field(default_factory=dict)
    raw_where: str | None = None

    def __post_init__(self) -> None:
        # --site and a --filter siteid=... condition both resolve to the same
        # underlying attribute; combining them silently either produces a
        # contradictory OSLC "and" (zero rows, no error) or, on REST MBO,
        # one silently overwrites the other via the same dict key. Reject the
        # ambiguity outright rather than guessing which one the caller meant.
        if self.site and any(name.lower() == "siteid" for name in self.conditions):
            raise ValueError(
                "conflicting site filter: --site and a siteid condition were both set; use only one"
            )


def quote_where_value(value: str) -> str:
    """Quote a value for embedding in an OSLC where clause.

    Rejects (rather than guesses how to escape) an embedded double quote —
    OSLC where syntax has no documented escaping convention verified for this
    tool, and interpolating one unescaped would let a value like
    `BEDFORD" or "1"="1` turn a scoping filter into an always-true predicate.

    Public (not just used by `Filter`/`to_oslc_where` here) — `load/importer.py`
    reuses it to build the natural-key where clause for create-or-update lookups.
    """
    if '"' in value:
        raise ValueError(f'filter value {value!r} contains a double quote, which cannot be safely embedded in an OSLC where clause')
    return f'"{value}"'


def to_oslc_where(default_where: str | None, filt: Filter | None) -> str | None:
    """Build an oslc.where expression, or None to send no filter at all."""
    if filt is None:
        return default_where
    if filt.raw_where:
        return filt.raw_where

    clauses: list[str] = []
    if default_where:
        clauses.append(f"({default_where})")
    if filt.site:
        clauses.append(f"siteid={quote_where_value(filt.site)}")
    for name, value in filt.conditions.items():
        clauses.append(f"{name}={quote_where_value(value)}")
    return " and ".join(clauses) if clauses else None


def to_rest_mbo_params(default_where: str | None, filt: Filter | None) -> dict[str, str]:
    """Build REST MBO {FIELD}={VALUE} query params. Field names are uppercased
    to match Maximo's attribute-name convention regardless of the (lowercase)
    convention used everywhere else in this tool's config/CLI.

    This is the single source of truth for "can REST MBO express this filter
    at all" — raises UnsupportedFilterError for `default_where` (an object's
    own YAML `where:`, which is an OSLC boolean expression) and for
    `raw_where`, since REST MBO has no boolean where-expression syntax.
    Callers (e.g. MaximoClient's fallback-eligibility check) should ask this
    function rather than re-deriving REST MBO's limits themselves.
    """
    if default_where:
        raise UnsupportedFilterError(
            "REST MBO cannot express this object's default `where` filter; use OSLC or clear it"
        )
    if filt is None:
        return {}
    if filt.raw_where:
        raise UnsupportedFilterError(
            "raw_where filters require OSLC; REST MBO cannot express an arbitrary where expression"
        )
    params: dict[str, str] = {}
    if filt.site:
        params["SITEID"] = filt.site
    for name, value in filt.conditions.items():
        params[name.upper()] = value
    return params
