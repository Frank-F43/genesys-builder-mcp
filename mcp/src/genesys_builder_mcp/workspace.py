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

STATE_FILE = Path(".genesys-builder") / "flow-state.json"
LEGACY_STATE_FILE = Path(".archy") / "flow-state.json"

# A flow YAML has exactly one top-level key, and that key is the flow type.
# The type names drop the "Flow" suffix and lowercase the rest, which holds for
# every type except the ones listed here.
YAML_KEY_EXCEPTIONS = {
    "workflow": "workflow",
    "workitem": "workitem",
}

FLOW_TYPES = {
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
    flow_type: str  # lowercase API spelling, e.g. "inboundshortmessage"

    @property
    def api_type(self) -> str:
        return self.flow_type.upper()


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repository root.

    ``GENESYS_BUILDER_REPO`` wins, so the server can be pointed at a specific
    clone. Otherwise walk up to the nearest git root.
    """
    configured = os.environ.get("GENESYS_BUILDER_REPO")
    if configured:
        root = Path(configured).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceError(f"GENESYS_BUILDER_REPO={root} is not a directory.")
        return root

    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate

    raise WorkspaceError(
        "Could not find the repository root. Set GENESYS_BUILDER_REPO to the "
        "clone directory in the MCP server environment."
    )


def flow_type_from_yaml_key(key: str) -> str:
    """Map a flow YAML's top-level key to its lowercase flow type name."""
    if key in YAML_KEY_EXCEPTIONS:
        return YAML_KEY_EXCEPTIONS[key]

    base = key[: -len("Flow")] if key.endswith("Flow") else key
    flow_type = base.lower()
    if flow_type not in FLOW_TYPES:
        raise WorkspaceError(
            f"Top-level key {key!r} does not map to a known flow type. "
            f"Expected one of: {', '.join(sorted(FLOW_TYPES))}"
        )
    return flow_type


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


def with_flow_name(text: str, new_name: str) -> str:
    """The same flow YAML under another name.

    Replaces the line ``read_local_flow`` takes the name from, so the copy is
    identified exactly as the original would be, just under the new name.
    """
    lines = text.splitlines(keepends=True)
    seen_key = False
    for index, line in enumerate(lines):
        if not seen_key:
            if line.strip() and not line.startswith("#"):
                seen_key = True
            continue
        stripped = line.lstrip()
        if stripped.startswith("name:"):
            indent = line[: len(line) - len(stripped)]
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"{indent}name: {json.dumps(new_name)}{ending}"
            return "".join(lines)
        if line.strip() and not line[0].isspace():
            break
    raise WorkspaceError("Could not find the flow name to replace.")


def iter_flow_files(root: Path) -> Iterator[Path]:
    """Every flow YAML in the repository, ignoring working directories."""
    skip = {".git", ".archy", ".genesys-builder", ".scratch", ".venv", "node_modules", "archive"}
    for path in sorted(root.rglob("*.yaml")):
        if any(part in skip for part in path.relative_to(root).parts):
            continue
        yield path


def load_state(root: Path) -> dict[str, Any]:
    """Read what the tools recorded about each flow file at export time."""
    path = root / STATE_FILE
    legacy = root / LEGACY_STATE_FILE
    if not path.is_file() and legacy.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(path)
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
