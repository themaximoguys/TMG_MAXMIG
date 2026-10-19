"""`migrate discover` — probe an unfamiliar object and draft its module YAML.

OSLC resource names mostly follow `mxapi{object}`, but not always (IBM's own
exceptions, plus customer object structures like `MXL_*` or `CUSTOMOS` that
follow no convention at all), and some environments don't publish an object
structure for a table at all. Rather than making the module author guess, the
probe tries the likely endpoints in a fixed order and reports what each one
answered, then turns the first working sample record into a DRAFT module
definition for a human to review.

The draft is never activated automatically: the natural key in particular
cannot be inferred from one sample record, and getting it wrong silently
breaks idempotent loading — so it is written as a best guess with a TODO.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import requests
import yaml

from maxmig.api.oslc import is_plumbing_key
from maxmig.api.http import join_url

ProbeStatus = Literal["ok", "not_found", "error"]
ProbeApi = Literal["oslc", "rest_mbo"]

# Maximo's "no such object / object structure" reason code. Some versions
# answer an unknown resource with a 400/500 carrying this code instead of a
# clean 404, so a status-code-only check would misreport "missing" as "broken".
_UNKNOWN_OBJECT_REASON_CODE = "BMXAA4216E"
_UNKNOWN_OBJECT_TEXT = "unknown object"

# Keys in a lean OSLC record that are API plumbing, not object attributes.

# Attributes that, when present, almost always belong in a Maximo natural key
# alongside the record's own number (site-, org- or itemset-scoped objects).
_SCOPE_KEY_FIELDS = ("siteid", "orgid", "itemsetid")


@dataclass(frozen=True)
class ProbeAttempt:
    """One endpoint the probe tried, and how it answered."""

    api: ProbeApi
    resource: str
    url: str
    status: ProbeStatus
    status_code: int | None = None
    message: str | None = None
    sample: dict[str, Any] | None = None


@dataclass
class DiscoveredRelation:
    """A child relation found nested in the sample record."""

    name: str
    attributes: list[str] = field(default_factory=list)
    custom_attributes: list[str] = field(default_factory=list)
    children: list[DiscoveredRelation] = field(default_factory=list)


@dataclass
class DiscoveryResult:
    """Every attempt made, plus the attribute shape read from the first one that worked."""

    name: str
    attempts: list[ProbeAttempt] = field(default_factory=list)
    working: ProbeAttempt | None = None
    attributes: list[str] = field(default_factory=list)
    custom_attributes: list[str] = field(default_factory=list)
    children: list[DiscoveredRelation] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.working is not None


# --------------------------------------------------------------------------- probing


def _error_signals_unknown_object(body: Any, text: str) -> bool:
    """True when a Maximo error payload says the object/resource doesn't exist.

    Handles the lean shape (`{"Error": {"reasonCode", "message"}}`), the
    non-lean OSLC shape (`{"oslc:Error": {"spi:reasonCode", "oslc:message"}}`),
    and plain-text/XML bodies (REST MBO) by falling back to a text search.
    """
    if isinstance(body, dict):
        for key in ("Error", "oslc:Error"):
            error = body.get(key)
            if isinstance(error, dict):
                code = str(error.get("reasonCode") or error.get("spi:reasonCode") or "")
                message = str(error.get("message") or error.get("oslc:message") or "")
                if code == _UNKNOWN_OBJECT_REASON_CODE or _UNKNOWN_OBJECT_TEXT in message.lower():
                    return True
    lowered = text.lower()
    return _UNKNOWN_OBJECT_REASON_CODE.lower() in lowered or _UNKNOWN_OBJECT_TEXT in lowered


def _error_message(body: Any, text: str) -> str:
    if isinstance(body, dict):
        for key in ("Error", "oslc:Error"):
            error = body.get(key)
            if isinstance(error, dict):
                message = error.get("message") or error.get("oslc:message")
                if message:
                    return str(message)
    return text[:300]


def _attempt(
    session: requests.Session,
    api: ProbeApi,
    resource: str,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any],
    timeout: int,
) -> ProbeAttempt:
    """Make one probe request and classify it as ok / not_found / error.

    Deliberately a plain `session.get`, not the retrying helper: a probe that
    hits a missing endpoint should move on immediately, not back off and retry
    a 404/500 that will never change.
    """
    try:
        response = session.get(url, headers=headers, params=params, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return ProbeAttempt(api, resource, url, "error", None, f"{type(exc).__name__}: {exc}")

    text = response.text or ""
    try:
        body: Any = response.json()
    except ValueError:
        body = None

    if 200 <= response.status_code < 300:
        if not isinstance(body, dict):
            # e.g. an SSO login page served with 200 after a redirect.
            return ProbeAttempt(api, resource, url, "error", response.status_code, "non-JSON 2xx response")
        if _has_error_body(body):
            status: ProbeStatus = "not_found" if _error_signals_unknown_object(body, "") else "error"
            return ProbeAttempt(api, resource, url, status, response.status_code, _error_message(body, text))
        return ProbeAttempt(api, resource, url, "ok", response.status_code, None, body)

    if response.status_code == 404 or _error_signals_unknown_object(body, text):
        return ProbeAttempt(api, resource, url, "not_found", response.status_code, _error_message(body, text))
    return ProbeAttempt(api, resource, url, "error", response.status_code, _error_message(body, text))


def _has_error_body(body: dict[str, Any]) -> bool:
    return isinstance(body.get("Error"), dict) or isinstance(body.get("oslc:Error"), dict)


def _candidates(name: str) -> list[tuple[ProbeApi, str]]:
    """Endpoints in probe order, de-duplicated (a name already starting with
    `mxapi` would otherwise be probed twice as OSLC)."""
    lowered = name.lower()
    oslc_names = [lowered if lowered.startswith("mxapi") else f"mxapi{lowered}", name]
    seen: set[str] = set()
    ordered: list[tuple[ProbeApi, str]] = []
    for resource in oslc_names:
        if resource.lower() not in seen:
            seen.add(resource.lower())
            ordered.append(("oslc", resource))
    ordered.append(("rest_mbo", lowered))
    return ordered


def probe_object(
    session: requests.Session,
    base_url: str,
    headers: dict[str, str],
    name: str,
    timeout: int = 60,
    custom_attributes: set[str] | None = None,
) -> DiscoveryResult:
    """Probe OSLC `mxapi{name}`, OSLC `{name}`, then REST MBO `{name}`; stop at the first that works.

    `custom_attributes` (e.g. from MAXATTRIBUTE where USERDEFINED=1) are split
    out of the attribute lists so the draft can list them under `custom_fields`
    — the sample record alone can't tell a customer's added column from IBM's.
    """
    custom = {attr.lower() for attr in (custom_attributes or set())}
    result = DiscoveryResult(name=name)

    for api, resource in _candidates(name):
        if api == "oslc":
            url = join_url(base_url, f"api/os/{resource}")
            params: dict[str, Any] = {"lean": 1, "oslc.pageSize": 1, "oslc.select": "*"}
        else:
            url = join_url(base_url, f"rest/mbo/{resource}")
            params = {"_format": "json", "_maxItems": 1}
        attempt = _attempt(session, api, resource, url, headers, params, timeout)
        result.attempts.append(attempt)
        if attempt.status == "ok":
            result.working = attempt
            break

    if result.working is None or result.working.sample is None:
        return result

    if result.working.api == "oslc":
        members = result.working.sample.get("member") or []
        record = members[0] if members and isinstance(members[0], dict) else {}
        attrs, relations = _shape_of_oslc_record(record, custom)
        result.attributes, result.custom_attributes = _split(attrs, custom)
        result.children = relations
    else:
        attrs = _rest_mbo_attributes(result.working.sample, result.working.resource)
        result.attributes, result.custom_attributes = _split(attrs, custom)
    return result


# --------------------------------------------------------------------------- shape extraction


_is_plumbing_key = is_plumbing_key


def _split(attrs: list[str], custom: set[str]) -> tuple[list[str], list[str]]:
    return [a for a in attrs if a not in custom], [a for a in attrs if a in custom]


def _shape_of_oslc_record(
    record: dict[str, Any], custom: set[str]
) -> tuple[list[str], list[DiscoveredRelation]]:
    """Split a lean OSLC record into its own scalar attributes and its nested relations.

    A value that is a list of dicts (or a single dict) is a child relation;
    every other value is a scalar attribute. Child attributes come from the
    union of all nested rows' keys, since one row may omit nulls another has.
    """
    attrs: list[str] = []
    relations: list[DiscoveredRelation] = []
    for raw_key, value in record.items():
        key = raw_key.lower()
        if _is_plumbing_key(key):
            continue
        rows: list[dict[str, Any]] | None = None
        if isinstance(value, dict):
            rows = [value]
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            rows = value
        if rows is None:
            if not isinstance(value, list):
                attrs.append(key)
            continue
        merged: dict[str, Any] = {}
        for row in rows:
            for k, v in row.items():
                merged.setdefault(k, v)
        child_attrs, grandchildren = _shape_of_oslc_record(merged, custom)
        standard, customs = _split(child_attrs, custom)
        relations.append(DiscoveredRelation(key, standard, customs, grandchildren))
    return attrs, relations


def _rest_mbo_attributes(body: dict[str, Any], table: str) -> list[str]:
    mbo_set = body.get(f"{table.upper()}MboSet", {})
    rows = mbo_set.get(table.upper(), []) if isinstance(mbo_set, dict) else []
    if not rows or not isinstance(rows[0], dict):
        return []
    return [key.lower() for key in rows[0].get("Attributes", {})]


# --------------------------------------------------------------------------- YAML drafting


def _flow(values: list[str]) -> str:
    """YAML flow sequence with correct quoting (e.g. an attribute named `on`
    would otherwise load as a boolean)."""
    return yaml.safe_dump(list(values), default_flow_style=True, width=2**31 - 1).strip()


def _guess_key_fields(attributes: list[str], object_lower: str) -> list[str]:
    """Best-guess natural key: the record's own `...num` attribute plus any scope columns.

    Prefers `{object}num` (ponum for PO) over the first `...num` seen, since
    records commonly carry other objects' numbers (itemnum on a PO line).
    """
    nums = [a for a in attributes if a.endswith("num")]
    primary = f"{object_lower}num" if f"{object_lower}num" in attributes else (nums[0] if nums else None)
    if primary is None:
        primary = attributes[0] if attributes else f"{object_lower}num"
    keys = [primary]
    keys.extend(scope for scope in _SCOPE_KEY_FIELDS if scope in attributes and scope != primary)
    return keys


def _guess_child_key(attributes: list[str], parent_keys: list[str]) -> str | None:
    candidates = [a for a in attributes if a not in parent_keys]
    for predicate in (
        lambda a: a.endswith("linenum"),
        lambda a: a.endswith("num"),
        lambda a: a.endswith("id"),
    ):
        for attr in candidates:
            if predicate(attr):
                return attr
    return candidates[0] if candidates else None


def _render_relations(relations: list[DiscoveredRelation], parent_keys: list[str], indent: int) -> list[str]:
    pad = " " * indent
    if not relations:
        return []
    lines: list[str] = []
    for rel in relations:
        child_key = _guess_child_key(rel.attributes + rel.custom_attributes, parent_keys)
        key_fields = parent_keys + ([child_key] if child_key else [])
        fields = [a for a in rel.attributes if a not in parent_keys]
        custom = [a for a in rel.custom_attributes if a not in parent_keys]
        lines.append(f"{pad}- name: {rel.name}")
        lines.append(f"{pad}  maximo_object: {rel.name.upper()}")
        lines.append(f"{pad}  relation_attribute: {rel.name}")
        lines.append(f"{pad}  foreign_key: {_flow(parent_keys)}")
        lines.append(f"{pad}  key_fields: {_flow(key_fields)}  # TODO: confirm natural key")
        lines.append(f"{pad}  fields: {_flow(fields)}")
        if custom:
            lines.append(f"{pad}  custom_fields: {_flow(custom)}")
        if rel.children:
            lines.append(f"{pad}  children:")
            lines.extend(_render_relations(rel.children, key_fields, indent + 4))
        else:
            lines.append(f"{pad}  children: []")
    return lines


def draft_module_yaml(result: DiscoveryResult, module_name: str, object_name: str) -> str:
    """Render a DRAFT module YAML (config/modules/_template.yaml schema) from a probe result.

    Raises ValueError if nothing answered — there is no shape to draft from.
    """
    working = result.working
    if working is None:
        raise ValueError(f"no endpoint answered for '{result.name}'; nothing to draft")

    maximo_object = result.name.upper()
    object_lower = result.name.lower()
    all_attrs = result.attributes + result.custom_attributes
    key_fields = _guess_key_fields(all_attrs, object_lower)

    if working.api == "oslc":
        access_lines = [
            f"      oslc_resource: {working.resource}",
            "      rest_mbo_table: null  # not probed; set to the table name if REST MBO is wanted",
            "      prefer: oslc",
        ]
    else:
        access_lines = [
            "      oslc_resource: null  # no OSLC object structure answered; REST MBO is read-only/flat",
            f"      rest_mbo_table: {working.resource}",
            "      prefer: rest_mbo",
        ]

    lines = [
        "# " + "=" * 76,
        f"# DRAFT generated by `migrate discover --object {result.name}`.",
        "# NOT auto-activated: review it, confirm the natural key (key_fields) and",
        "# trim the field list, then move it into config/modules/ or",
        "# config/custom_objects/ under a name WITHOUT the .discovered suffix.",
        "#",
        "# Probe results:",
    ]
    for attempt in result.attempts:
        code = attempt.status_code if attempt.status_code is not None else "-"
        detail = f" ({attempt.message})" if attempt.message else ""
        lines.append(f"#   {attempt.api:<8} {attempt.resource:<28} {attempt.status} [{code}]{detail}"[:200])
    lines += [
        "# " + "=" * 76,
        f"module: {module_name}",
        f"description: {json.dumps(f'DRAFT discovered from {working.api} {working.resource}')}",
        "objects:",
        f"  - name: {object_name}",
        f"    maximo_object: {maximo_object}",
        "    access:",
        *access_lines,
        "    # TODO: confirm natural key",
        f"    key_fields: {_flow(key_fields)}",
        f"    fields: {_flow(result.attributes)}",
    ]
    if result.custom_attributes:
        lines.append(f"    custom_fields: {_flow(result.custom_attributes)}")
    lines.append("    where: null")
    if result.children:
        lines.append("    children:")
        lines.extend(_render_relations(result.children, key_fields, 6))
    else:
        lines.append("    children: []")
    lines += [
        f"extract_order: {_flow([object_name])}",
        f"load_order: {_flow([object_name])}",
    ]
    return "\n".join(lines) + "\n"


def write_draft(path_dir: Path, object_name: str, yaml_text: str, overwrite: bool = False) -> Path:
    """Write `{object_name}.discovered.yaml` into `path_dir`.

    Refuses to clobber an existing draft by default: the user may already have
    hand-edited it (fixed the key, trimmed fields), and a re-probe shouldn't
    silently discard that work.
    """
    path_dir = Path(path_dir)
    path_dir.mkdir(parents=True, exist_ok=True)
    path = path_dir / f"{object_name}.discovered.yaml"
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists; pass overwrite=True to replace it")
    path.write_text(yaml_text, encoding="utf-8")
    return path
