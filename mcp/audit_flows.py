"""Report how each local flow YAML differs from what is published, without
touching the local files.

Exports every flow to a scratch directory and diffs. A blanket re-export would
be the obvious way to resync, but it would also silently destroy unpublished
local edits, which is the loss this whole check exists to prevent.

    PYTHONPATH=src .venv/bin/python audit_flows.py
"""

from __future__ import annotations

import difflib
import logging
import sys
import tempfile
from pathlib import Path

logging.disable(logging.INFO)

from genesys_builder_mcp import archy  # noqa: E402
from genesys_builder_mcp.flowdiff import normalise  # noqa: E402
from genesys_builder_mcp.workspace import find_repo_root, iter_flow_files, read_local_flow  # noqa: E402


def main() -> int:
    root = find_repo_root()

    # Re-exporting all flows takes minutes, so a previous run's directory can be
    # passed in to iterate on the comparison itself.
    reuse = len(sys.argv) > 1
    scratch = Path(sys.argv[1]) if reuse else Path(tempfile.mkdtemp(prefix="flow-audit-"))
    print(f"{'reusing exports in' if reuse else 'exporting to'} {scratch}\n")

    rows = []
    for path in iter_flow_files(root):
        local = read_local_flow(path)
        name = local.name
        exported = scratch / f"{name}.yaml"

        if not reuse:
            try:
                archy.run(
                    root,
                    "export",
                    [
                        "--flowName", name,
                        "--flowType", local.flow_type,
                        "--exportType", "yaml",
                        "--outputDir", str(scratch),
                        "--exportFileName", f"{name}.yaml",
                        "--force",
                    ],
                )
            except archy.ArchyError as exc:
                rows.append((name, "export failed", str(exc)[:80]))
                continue

        local_text = path.read_text(encoding="utf-8")
        org_text = exported.read_text(encoding="utf-8")

        if local_text == org_text:
            rows.append((name, "identical", ""))
            continue

        local_sorted, org_sorted = normalise(local_text), normalise(org_text)
        if local_sorted == org_sorted:
            rows.append((name, "formatting only", ""))
            continue

        only_local = sum(1 for line in difflib.unified_diff(org_sorted, local_sorted) if line.startswith("+"))
        only_org = sum(1 for line in difflib.unified_diff(org_sorted, local_sorted) if line.startswith("-"))
        rows.append((name, "CONTENT DIFFERS", f"{only_local} lines only local, {only_org} only in org"))

    width = max(len(r[0]) for r in rows)
    print(f"{'flow'.ljust(width)}  verdict")
    for name, verdict, detail in rows:
        print(f"{name.ljust(width)}  {verdict}  {detail}")

    print(f"\nexports kept in {scratch} for inspection")
    return 0


if __name__ == "__main__":
    sys.exit(main())
