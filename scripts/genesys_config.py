"""Genesys Cloud credential resolution for standalone toolkit scripts.

Resolution order (same as ``genesys_builder_mcp.config``):

1. Environment variables ``GENESYSCLOUD_REGION``, ``GENESYSCLOUD_OAUTHCLIENT_ID``,
   ``GENESYSCLOUD_OAUTHCLIENT_SECRET``.
2. ``~/.archy_config`` — Archy stores client credentials there.

Returns a dict in Archy's key shape: ``location``, ``clientId``, ``clientSecret``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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


class ConfigError(Exception):
    pass


def _from_env() -> dict[str, str] | None:
    region = os.environ.get("GENESYSCLOUD_REGION")
    client_id = os.environ.get("GENESYSCLOUD_OAUTHCLIENT_ID")
    client_secret = os.environ.get("GENESYSCLOUD_OAUTHCLIENT_SECRET")
    if region and client_id and client_secret:
        return {
            "location": region,
            "clientId": client_id,
            "clientSecret": client_secret,
        }
    return None


def _from_archy_config() -> dict[str, str] | None:
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
        return {
            "location": region,
            "clientId": client_id,
            "clientSecret": client_secret,
        }
    return None


def load_config() -> dict[str, str]:
    """Load credentials; raise :class:`ConfigError` when none are configured."""
    cfg = _from_env() or _from_archy_config()
    if cfg is None:
        raise ConfigError(
            "No Genesys Cloud credentials found. Set GENESYSCLOUD_REGION, "
            "GENESYSCLOUD_OAUTHCLIENT_ID and GENESYSCLOUD_OAUTHCLIENT_SECRET "
            f"in the environment, or configure Archy ({ARCHY_CONFIG})."
        )
    region = cfg["location"]
    if region not in VALID_REGIONS:
        raise ConfigError(
            f"Unknown Genesys Cloud region {region!r}. "
            f"Expected one of: {', '.join(sorted(VALID_REGIONS))}"
        )
    return cfg
