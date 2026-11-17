"""Escape hatch for endpoints that have no dedicated tool.

Genesys Cloud has well over a thousand endpoints. Exposing them all as tools
would swamp the model's context, so the curated tools cover the frequent work
and this covers the rest.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import DESTRUCTIVE
from ..client import get_client

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def register(mcp: MCPServer) -> None:
    # Marked destructive because it can reach delete endpoints, even though most
    # calls through it only read. The confirmation gate below is a check on
    # accidents, not something a client should rely on to auto-approve.
    @mcp.tool(annotations=DESTRUCTIVE)
    def genesys_api_call(
        method: str,
        path: str,
        body: dict[str, Any] | list[Any] | None = None,
        query: dict[str, Any] | None = None,
        confirm_delete_path: str | None = None,
    ) -> Any:
        """Call any Genesys Cloud Platform API endpoint directly.

        Use this only when no dedicated tool covers the task — the dedicated
        tools encode multi-step sequences (validate before publish, poll upload
        status) that are easy to get wrong by hand.

        Args:
            method: GET, POST, PUT, PATCH or DELETE.
            path: Path starting with /api/v2, for example /api/v2/routing/queues
            body: JSON request body for POST, PUT and PATCH — an object **or** a
                list. Some routing endpoints expect a top-level array, e.g.
                ``POST /api/v2/routing/queues/{queueId}/wrapupcodes`` with
                ``[{"id": "<wrapupCodeId>"}]``.
            query: Query string parameters.
            confirm_delete_path: Required for DELETE, and must repeat path
                exactly. Prefer delete_object, which resolves a name and tells
                you what the deletion takes with it; this is for the endpoints
                it does not cover.
        """
        # ToolError, not ValueError: the framework treats anything else as a crash
        # and drops the message, leaving the caller a bare "Error executing tool"
        # with no way to see what to correct. See docs/error-reporting-findings.md.
        method = method.upper()
        if method not in ALLOWED_METHODS:
            raise ToolError(
                f"Method {method} is not allowed. Supported: {', '.join(sorted(ALLOWED_METHODS))}."
            )
        if method == "DELETE" and confirm_delete_path != path:
            raise ToolError(
                "Deleting through the raw endpoint needs confirm_delete_path to repeat the path "
                f"exactly. Pass confirm_delete_path={path!r} if that is really what should be "
                "deleted. Nothing was deleted."
            )
        if not path.startswith("/api/v2"):
            raise ToolError(f"Path must start with /api/v2, got {path!r}")

        return get_client().request(method, path, json_body=body, params=query)
