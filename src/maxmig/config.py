"""Environment/credential loading — reads {PREFIX}_MANAGE_* vars from a dotenv file.

Follows the org-wide convention already in use under
/path/to/credentials/.env.* — this module intentionally
does not talk to Vaultwarden or any other secret store; the dotenv file is the
runtime source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")


class MissingEnvVarError(RuntimeError):
    """Raised when a required {PREFIX}_MANAGE_* variable is absent from a dotenv file."""

    def __init__(self, var_name: str, path: Path):
        self.var_name = var_name
        self.path = path
        super().__init__(f"Missing required env var {var_name!r} in {path}")


@dataclass(frozen=True)
class EnvConfig:
    prefix: str
    manage_url: str
    username: str | None = None
    password: str | None = None
    api_key: str | None = None


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE dotenv file, stripping full-line and inline `#` comments."""
    values: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = _INLINE_COMMENT_RE.sub("", val).strip()
        values[key] = val
    return values


def resolve_env(prefix: str, values: dict[str, str], path: Path) -> EnvConfig:
    """Build an EnvConfig for `prefix` out of an already-parsed dotenv dict.

    MANAGE_APIKEY is optional and preferred when present (some MAS instances
    are SSO/OIDC-fronted and don't accept a bare maxauth basic-auth header at
    all — an API key, generated once via the Manage UI, is the only headless
    auth path for those). When no API key is configured, MANAGE_USERNAME and
    MANAGE_PASSWORD are required as before.
    """
    prefix_upper = prefix.upper()
    # Case-insensitive lookup: uppercase every key. If the same var appears with
    # different casing, the last one (by file order) wins — matches normal
    # dotenv/shell semantics where a later assignment overrides an earlier one.
    upper_values = {k.upper(): v for k, v in values.items()}

    def require(suffix: str) -> str:
        var_name = f"{prefix_upper}_{suffix}"
        if var_name not in upper_values or not upper_values[var_name]:
            raise MissingEnvVarError(var_name, path)
        return upper_values[var_name]

    def optional(suffix: str) -> str | None:
        return upper_values.get(f"{prefix_upper}_{suffix}") or None

    api_key = optional("MANAGE_APIKEY")
    if api_key:
        username = optional("MANAGE_USERNAME")
        password = optional("MANAGE_PASSWORD")
    else:
        username = require("MANAGE_USERNAME")
        password = require("MANAGE_PASSWORD")

    return EnvConfig(
        prefix=prefix_upper,
        manage_url=require("MANAGE_URL"),
        username=username,
        password=password,
        api_key=api_key,
    )


def load_env(prefix: str, path: Path) -> EnvConfig:
    """Load a dotenv file and resolve the {PREFIX}_MANAGE_* vars for `prefix`."""
    values = parse_dotenv(path)
    return resolve_env(prefix, values, path)
