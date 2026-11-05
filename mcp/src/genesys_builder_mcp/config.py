"""Credential and region resolution.

Environment variables, using the same names as the other Genesys Cloud MCP
servers in the wild, so a colleague who already configured one of those can
reuse the values verbatim. They belong in the env block of the project's
``.cursor/mcp.json``, which makes the org a property of the project.

Resolution order:

1. The project's ``.cursor/mcp.json``, found from ``GENESYS_BUILDER_REPO`` or
   the working directory upwards. Scripts run from a terminal inside the
   project - the smoke test, the NLU probe, the flow audit - land here, and a
   shell exporting other values cannot redirect them.
2. The environment. That is how the editor hands the server the env block of
   the configuration that started it - the authoritative source for the server,
   whose working directory is the toolkit, not the project.
3. ``~/.cursor/mcp.json``, Cursor's global configuration, for terminal scripts
   of a setup configured there. It comes last because it may describe another
   org than the project at hand.

There is deliberately no machine-wide fallback such as ``~/.archy_config``: a
project without its own credentials would silently talk to whichever org that
file points at, and publish into it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

REGION_NAMES = {
    "apne2.pure.cloud": "Asia Pacific (Seoul)",
    "apne3.pure.cloud": "Asia Pacific (Osaka)",
    "aps1.pure.cloud": "Asia Pacific (Mumbai)",
    "apse1.pure.cloud": "Asia Pacific (Singapore)",
    "cac1.pure.cloud": "Canada (Central)",
    "euc2.pure.cloud": "EU (Zurich)",
    "edee1.eusc-pure.cloud": "EU Sovereign Cloud (Brandenburg)",
    "euw2.pure.cloud": "EU (London)",
    "mec1.pure.cloud": "Middle East (UAE)",
    "mxc1.pure.cloud": "Americas (Mexico)",
    "mypurecloud.com": "Americas (US East)",
    "mypurecloud.com.au": "Asia Pacific (Sydney)",
    "mypurecloud.de": "EU (Frankfurt)",
    "mypurecloud.ie": "EU (Ireland)",
    "mypurecloud.jp": "Asia Pacific (Tokyo)",
    "sae1.pure.cloud": "Americas (Sao Paulo)",
    "use2.us-gov-pure.cloud": "Americas (US East 2, FedRAMP)",
    "usw2.pure.cloud": "Americas (US West)",
}
VALID_REGIONS = set(REGION_NAMES)


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


PROJECT_CONFIG = Path(".cursor") / "mcp.json"
SERVER_NAME = "genesys-builder"


def _from_values(values: dict[str, str], source: str) -> Config | None:
    region = values.get("GENESYSCLOUD_REGION")
    client_id = values.get("GENESYSCLOUD_OAUTHCLIENT_ID")
    client_secret = values.get("GENESYSCLOUD_OAUTHCLIENT_SECRET")
    if region and client_id and client_secret:
        return Config(region, client_id, client_secret, source=source)
    return None


GLOBAL_CONFIG = Path.home() / PROJECT_CONFIG


def find_project_config() -> Path | None:
    """The nearest project .cursor/mcp.json, from GENESYS_BUILDER_REPO or the working directory up."""
    starts = [Path(os.environ["GENESYS_BUILDER_REPO"]).expanduser()] if os.environ.get("GENESYS_BUILDER_REPO") else []
    starts.append(Path.cwd())
    for start in starts:
        start = start.resolve()
        for directory in [start, *start.parents]:
            candidate = directory / PROJECT_CONFIG
            if candidate.is_file() and candidate != GLOBAL_CONFIG:
                return candidate
    return None


def _from_config_file(path: Path | None) -> Config | None:
    if path is None or not path.is_file():
        return None
    try:
        servers = json.loads(path.read_text(encoding="utf-8")).get("mcpServers") or {}
    except (OSError, json.JSONDecodeError):
        return None
    return _from_values((servers.get(SERVER_NAME) or {}).get("env") or {}, source=str(path))


def load_config() -> Config:
    config = (
        _from_config_file(find_project_config())
        or _from_values(dict(os.environ), source="environment")
        or _from_config_file(GLOBAL_CONFIG)
    )
    if config is None:
        raise ConfigError(
            "No Genesys Cloud credentials found. Set GENESYSCLOUD_REGION, "
            "GENESYSCLOUD_OAUTHCLIENT_ID and GENESYSCLOUD_OAUTHCLIENT_SECRET "
            "in the env block of this project's .cursor/mcp.json."
        )
    if config.region not in VALID_REGIONS:
        raise ConfigError(
            f"Unknown Genesys Cloud region {config.region!r} (from {config.source}). "
            f"Expected one of: {', '.join(sorted(VALID_REGIONS))}"
        )
    return config
