"""Credential and region resolution.

Two sources, in order of precedence:

1. Environment variables, using the same names as the other Genesys Cloud MCP
   servers in the wild, so a colleague who already configured one of those can
   reuse the values verbatim.
2. ``~/.archy_config`` — Archy stores client credentials there, so anyone who
   already uses Archy is configured without doing anything.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

ARCHY_CONFIG = Path.home() / ".archy_config"

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


class ConfigError(ToolError):
    pass


@dataclass(frozen=True)
class Config:
    region: str
    client_id: str
    client_secret: str
    source: str

    @property
    def api_base(self) -> str:
        return f"https://api.{self.region}"

    @property
    def login_base(self) -> str:
        return f"https://login.{self.region}"

    @property
    def apps_base(self) -> str:
        """App domain. Some endpoints (script upload) only exist here."""
        return f"https://apps.{self.region}"


def _from_env() -> Config | None:
    region = os.environ.get("GENESYSCLOUD_REGION")
    client_id = os.environ.get("GENESYSCLOUD_OAUTHCLIENT_ID")
    client_secret = os.environ.get("GENESYSCLOUD_OAUTHCLIENT_SECRET")
    if region and client_id and client_secret:
        return Config(region, client_id, client_secret, source="environment")
    return None


def _from_archy_config() -> Config | None:
    if not ARCHY_CONFIG.is_file():
        return None
    try:
        data = json.loads(ARCHY_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    region = data.get("location")
    client_id = data.get("clientId")
    client_secret = data.get("clientSecret")
    if region and client_id and client_secret:
        return Config(region, client_id, client_secret, source=str(ARCHY_CONFIG))
    return None


def load_config() -> Config:
    config = _from_env() or _from_archy_config()
    if config is None:
        raise ConfigError(
            "No Genesys Cloud credentials found. Set GENESYSCLOUD_REGION, "
            "GENESYSCLOUD_OAUTHCLIENT_ID and GENESYSCLOUD_OAUTHCLIENT_SECRET "
            f"in the MCP server environment, or configure Archy ({ARCHY_CONFIG})."
        )
    if config.region not in VALID_REGIONS:
        raise ConfigError(
            f"Unknown Genesys Cloud region {config.region!r} (from {config.source}). "
            f"Expected one of: {', '.join(sorted(VALID_REGIONS))}"
        )
    return config
