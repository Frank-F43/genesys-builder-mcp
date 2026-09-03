"""Locating the repository and the flow files inside it.

The MCP server is started by the editor, so its working directory is not
something to rely on. The repository is found explicitly instead.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from mcp.server.mcpserver.exceptions import ToolError

STATE_FILE = Path(".archy") / "flow-state.json"

# A flow YAML has exactly one top-level key, and that key is the flow type.
# Archy's own type names drop the "Flow" suffix and lowercase the rest, which
# holds for every type except the ones listed here.
YAML_KEY_EXCEPTIONS = {
    "workflow": "workflow",
    "workitem": "workitem",
}

ARCHY_FLOW_TYPES = {
    "bot",
    "commonmodule",
    "digitalbot",
    "emailsend",
    "inboundcall",
    "inboundchat",
    "inboundemail",
    "inboundshortmessage",
    "inqueuecall",
    "inqueueemail",
    "inqueueshortmessage",
    "outboundcall",
    "securecall",
    "surveyinvite",
    "voice",
    "voicemail",
    "voicesurvey",
    "workflow",
    "workitem",
}


class WorkspaceError(ToolError):
    pass


@dataclass(frozen=True)
class LocalFlow:
    """A flow YAML on disk, identified by its own contents rather than its path."""

    path: Path
    name: str
    flow_type: str  # Archy spelling, e.g. "inboundshortmessage"

    @property
    def api_type(self) -> str:
        return self.flow_type.upper()


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root.

    ``GENESYS_BUILDER_REPO`` wins, so the server can be pointed at a specific
    clone. Otherwise walk up looking for the ``archy`` wrapper, which only
    exists at the root.
    """
    configured = os.environ.get("GENESYS_BUILDER_REPO")
    if configured:
        root = Path(configured).expanduser().resolve()
        if not (root / "archy").is_file():
            raise WorkspaceError(f"GENESYS_BUILDER_REPO={root} does not contain the archy wrapper.")
        return root

    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "archy").is_file() and (candidate / ".git").exists():
            return candidate

    raise WorkspaceError(
        "Could not find the repository root. Set GENESYS_BUILDER_REPO to the "
        "clone directory in the MCP server environment."
    )


def flow_type_from_yaml_key(key: str) -> str:
    """Map a flow YAML's top-level key to Archy's flow type name."""
    if key in YAML_KEY_EXCEPTIONS:
        return YAML_KEY_EXCEPTIONS[key]

    base = key[: -len("Flow")] if key.endswith("Flow") else key
    archy_type = base.lower()
    if archy_type not in ARCHY_FLOW_TYPES:
        raise WorkspaceError(
            f"Top-level key {key!r} does not map to a known flow type. "
            f"Expected one of: {', '.join(sorted(ARCHY_FLOW_TYPES))}"
        )
    return archy_type


def read_local_flow(path: Path) -> LocalFlow:
    """Identify a flow YAML from its own header.

    Only the header is parsed. These files reach several megabytes and a full
    YAML parse costs seconds, while everything needed is in the first lines.
    """
    if not path.is_file():
        raise WorkspaceError(f"Flow file not found: {path}")

    key: str | None = None
    name: str | None = None
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if key is None:
                if not line.strip() or line.startswith("#"):
                    continue
                if line[0].isspace() or ":" not in line:
                    raise WorkspaceError(f"{path} does not start with a flow type key.")
                key = line.split(":", 1)[0].strip()
                continue

            stripped = line.strip()
            if stripped.startswith("name:"):
                name = stripped[len("name:") :].strip().strip('"').strip("'")
                break
            if not line[0].isspace():
                break

    if not key or not name:
        raise WorkspaceError(f"Could not read a flow type and name from {path}.")

    return LocalFlow(path=path, name=name, flow_type=flow_type_from_yaml_key(key))


def iter_flow_files(root: Path) -> Iterator[Path]:
    """Every flow YAML in the repository, ignoring working directories."""
    skip = {".git", ".archy", ".venv", "node_modules", "archive"}
    for path in sorted(root.rglob("*.yaml")):
        if any(part in skip for part in path.relative_to(root).parts):
            continue
        yield path


def load_state(root: Path) -> dict[str, Any]:
    """Read what the tools recorded about each flow file at export time."""
    path = root / STATE_FILE
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(root: Path, state: dict[str, Any]) -> None:
    path = root / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
