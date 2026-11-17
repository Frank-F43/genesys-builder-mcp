"""Shared tool annotations.

Marking which tools only read matters here: these tools act on a live Genesys
Cloud org, and the client uses these hints to decide what it can run without
asking.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)

# Creates or changes configuration, but never removes anything.
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

# Can remove something: delete_object, the raw API call now that it reaches
# DELETE endpoints, and the External Contact reset, which deletes and recreates
# the contact to clear its identity-stitching history.
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
