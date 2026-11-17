"""Genesys Cloud credential resolution for standalone toolkit scripts.

Resolution order:

1. The ``env`` block of the ``genesys-builder`` server in the nearest
   ``.cursor/mcp.json``, searched upwards from the working directory and then
   from this file. That is where the MCP server gets the same values, so a
   project's scripts and its server always talk to the same org - even when a
   shell happens to export other values.
2. Environment variables ``GENESYSCLOUD_REGION``, ``GENESYSCLOUD_OAUTHCLIENT_ID``,
   ``GENESYSCLOUD_OAUTHCLIENT_SECRET``, only where no such file is found.

Returns a dict with ``location``, ``clientId`` and ``clientSecret``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MCP_CONFIG = Path(".cursor") / "mcp.json"
SERVER_NAME = "genesys-builder"
ENV_KEYS = ("GENESYSCLOUD_REGION", "GENESYSCLOUD_OAUTHCLIENT_ID", "GENESYSCLOUD_OAUTHCLIENT_SECRET")

VALID_REGIONS = {
    "apne2.pure.cloud",
    "apne3.pure.cloud",
    "aps1.pure.cloud",
    "apse1.pure.cloud",
    "cac1.pure.cloud",
    "euc2.pure.cloud",
    "edee1.eusc-pure.cloud",
    "euw2.pure.cloud",
    "mec1.pure.cloud",
    "mxc1.pure.cloud",
    "mypurecloud.com",
    "mypurecloud.com.au",
    "mypurecloud.de",
    "mypurecloud.ie",
    "mypurecloud.jp",
    "sae1.pure.cloud",
    "use2.us-gov-pure.cloud",
    "usw2.pure.cloud",
}


class ConfigError(Exception):
    pass


def _as_config(values: dict[str, str]) -> dict[str, str] | None:
    region, client_id, client_secret = (values.get(key) for key in ENV_KEYS)
    if region and client_id and client_secret:
        return {"location": region, "clientId": client_id, "clientSecret": client_secret}
    return None


def find_mcp_config() -> Path | None:
    for start in (Path.cwd().resolve(), Path(__file__).resolve().parent):
        for candidate in [start, *start.parents]:
            path = candidate / MCP_CONFIG
            if path.is_file():
                return path
    return None


def _mcp_env() -> dict[str, str]:
    path = find_mcp_config()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return ((data.get("mcpServers") or {}).get(SERVER_NAME) or {}).get("env") or {}


def load_config() -> dict[str, str]:
    """Load credentials; raise :class:`ConfigError` when none are configured."""
    cfg = _as_config(_mcp_env()) if find_mcp_config() else _as_config(dict(os.environ))
    if cfg is None:
        raise ConfigError(
            "No Genesys Cloud credentials found. Set GENESYSCLOUD_REGION, "
            "GENESYSCLOUD_OAUTHCLIENT_ID and GENESYSCLOUD_OAUTHCLIENT_SECRET in the "
            f"{SERVER_NAME} env block of the project's {MCP_CONFIG} "
            f"({find_mcp_config() or 'none found'})."
        )
    region = cfg["location"]
    if region not in VALID_REGIONS:
        raise ConfigError(
            f"Unknown Genesys Cloud region {region!r}. "
            f"Expected one of: {', '.join(sorted(VALID_REGIONS))}"
        )
    return cfg


def load_region() -> str | None:
    """The configured region alone, for callers that need no credentials."""
    if find_mcp_config():
        return _mcp_env().get("GENESYSCLOUD_REGION")
    return os.environ.get("GENESYSCLOUD_REGION")
