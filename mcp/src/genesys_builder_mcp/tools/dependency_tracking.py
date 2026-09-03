"""Architect's dependency tracking index.

The org keeps an index of what references what: which flow calls which data
action, which bot flow uses which knowledge base. Deleting anything that could
be referenced consults it first.

Publishing flows invalidates the index. Once it is stale, the platform can no
longer answer "does a bot flow use this knowledge base?", and rather than
guessing it refuses the deletion with

    424 knowledgebase.bot.flow.status.unknown ... status unknown

which reads like the object is in use when the truth is that nothing knows. The
Genesys Cloud UI shows the same condition as "Server Error Occurred. Please try
again after some time." - waiting does not help, because the rebuild only
happens when asked for.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..annotations import READ_ONLY, WRITES
from ..client import get_client

BUILD_PATH = "/api/v2/architect/dependencytracking/build"

STATUS_MEANING = {
    "OPERATIONAL": "Current. Deletions that check references will work.",
    "OPERATIONALNEEDSREBUILD": "Stale. Deletions may be refused with 'status unknown'. Rebuild it.",
    "BUILDINPROGRESS": "Rebuilding now. Wait for it to finish.",
    "NOTBUILT": "Never built. Rebuild it.",
}

POLL_SECONDS = 15
POLL_ATTEMPTS = 60


def register(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY)
    def dependency_tracking_status() -> dict[str, Any]:
        """Check whether the org's dependency index is current.

        Worth checking when a deletion is refused for a reason that does not
        match what you can see, or after publishing a batch of flows.
        """
        build = get_client().get(BUILD_PATH)
        status = build.get("status")
        return {
            "status": status,
            "means": STATUS_MEANING.get(status, "Unrecognised status."),
            "last_completed": build.get("dateCompleted"),
            "failed_objects": build.get("failedObjects") or [],
        }

    @mcp.tool(annotations=WRITES)
    def rebuild_dependency_tracking(wait: bool = True) -> dict[str, Any]:
        """Rebuild the dependency index, then wait for it to finish.

        Reindexes what references what. It removes nothing and changes no
        configuration. Took under two minutes on an org with a few hundred
        objects; larger orgs take longer.

        Args:
            wait: Poll until the build finishes. With False it returns as soon
                as the rebuild has been accepted.
        """
        client = get_client()
        client.request("POST", BUILD_PATH)

        if not wait:
            return {"triggered": True, "waited": False}

        for attempt in range(POLL_ATTEMPTS):
            build = client.get(BUILD_PATH)
            status = build.get("status")
            if status == "OPERATIONAL":
                return {
                    "triggered": True,
                    "waited": True,
                    "status": status,
                    "took_about_seconds": attempt * POLL_SECONDS,
                    "last_completed": build.get("dateCompleted"),
                    "failed_objects": build.get("failedObjects") or [],
                }
            time.sleep(POLL_SECONDS)

        raise ToolError(
            f"Still {status!r} after {POLL_ATTEMPTS * POLL_SECONDS}s. "
            "It may simply be a large org; check dependency_tracking_status."
        )
