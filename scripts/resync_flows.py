"""Re-export every tracked flow into place, recording its published version.

One-off, for bringing a repository whose files predate these tools onto a
baseline the freshness check can reason about. Names given as arguments are
skipped, for flows with unpublished local edits worth keeping.

    PYTHONPATH=src .venv/bin/python resync_flows.py "Support Bot V2"
"""

from __future__ import annotations

import asyncio
import logging
import sys

logging.disable(logging.INFO)

from genesys_builder_mcp.server import mcp  # noqa: E402
from genesys_builder_mcp.workspace import find_repo_root, iter_flow_files, read_local_flow  # noqa: E402


async def main() -> int:
    skip = set(sys.argv[1:])
    root = find_repo_root()
    failures = 0

    for path in iter_flow_files(root):
        local = read_local_flow(path)
        if local.name in skip:
            print(f"  skip    {local.name}")
            continue

        result = await mcp.call_tool(
            "export_flow",
            {"flow_name": local.name, "flow_type": local.flow_type},
        )
        if result.is_error:
            print(f"  FAILED  {local.name}: {result.content[0].text.splitlines()[-1]}")
            failures += 1
            continue

        version = (result.structured_content or {}).get("org_version")
        print(f"  ok      {local.name} at {version}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
